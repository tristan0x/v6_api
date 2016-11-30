import os
import functools
import requests

from c2corg_api.models import DBSession
from c2corg_api.models.document_history import has_been_created_by
from c2corg_api.models.feed import update_feed_images_upload
from c2corg_api.models.image import Image, schema_image, schema_update_image, \
    IMAGE_TYPE, schema_create_image, schema_create_image_list
from c2corg_api.views.document_info import DocumentInfoRest
from c2corg_api.views.document_schemas import image_documents_config
from c2corg_common.fields_image import fields_image
from cornice.resource import resource, view
from cornice.validators import colander_body_validator

from c2corg_api.views.document import DocumentRest, make_validator_create, \
    make_validator_update, validate_document
from c2corg_api.views import cors_policy, restricted_json_view
from c2corg_api.views import set_creator as set_creator_on_documents
from c2corg_api.views.validation import validate_id, validate_pagination, \
    validate_lang_param, validate_preferred_lang_param, \
    validate_associations, validate_associations_in, validate_lang

from pyramid.httpexceptions import HTTPForbidden, HTTPNotFound, \
    HTTPBadRequest, HTTPInternalServerError, HTTPFound


def check_filename_unique(image, request, updating):
    """Checks that filename is unique
    """
    if 'filename' not in image:
        return
    sql = DBSession.query(Image) \
        .filter(Image.filename == image['filename'])
    if updating:
        sql = sql.filter(Image.document_id != image['document_id'])
    if sql.count() > 0:
        request.errors.add('body', 'filename', 'Unique')


def make_validator_filename_unique(updating):
    if updating:
        def f(request, **kwargs):
            image = request.validated
            check_filename_unique(image, request, updating=True)
    else:
        def f(request, **kwargs):
            image = request.validated
            check_filename_unique(image, request, updating=False)
    return f


base_validate_image_create = make_validator_create(
        fields_image.get('required'))
base_validate_image_update = make_validator_update(
        fields_image.get('required'))
validate_filename_create = make_validator_filename_unique(updating=False)
validate_filename_update = make_validator_filename_unique(updating=True)


def validate_image_create(request, **kwargs):
    base_validate_image_create(request, **kwargs)
    validate_filename_create(request, **kwargs)


def validate_image_update(request, **kwargs):
    base_validate_image_update(request, **kwargs)
    validate_filename_update(request, **kwargs)


validate_associations_create = functools.partial(
    validate_associations, IMAGE_TYPE, True)
validate_associations_update = functools.partial(
    validate_associations, IMAGE_TYPE, False)


def validate_list_image_create(request, **kwargs):
    for image in request.validated['images']:
        validate_document(image, request, fields_image.get('required'),
                          updating=False)
        check_filename_unique(image, request, updating=False)


def validate_list_associations_create(request, **kwargs):
    for document in request.validated['images']:
        associations_in = document.get('associations', None)
        if not associations_in:
            continue
        document['associations'] = validate_associations_in(
            associations_in, IMAGE_TYPE, request.errors)


def create_image(self, document_in):
    document = self._create_document(document_in, schema_image)

    settings = self.request.registry.settings
    url = '{}/{}'.format(settings['image_backend.url'], 'publish')
    response = requests.post(
            url,
            data={'secret': settings['image_backend.secret_key'],
                  'filename': document.filename})
    if response.status_code != 200:
        raise HTTPInternalServerError('Image backend returns : {} {}'.
                                      format(response.status_code,
                                             response.reason))
    return document


@resource(collection_path='/images', path='/images/{id}',
          cors_policy=cors_policy)
class ImageRest(DocumentRest):

    @view(validators=[validate_pagination, validate_preferred_lang_param])
    def collection_get(self):
        return self._collection_get(IMAGE_TYPE, image_documents_config)

    @view(validators=[validate_id, validate_lang_param])
    def get(self):
        return self._get(Image, schema_image, set_custom_fields=set_creator)

    @restricted_json_view(
            schema=schema_create_image,
            validators=[
                colander_body_validator,
                validate_image_create,
                validate_associations_create])
    def collection_post(self):
        document_in = self.request.validated
        document = create_image(self, document_in)
        return {'document_id': document.document_id}

    @restricted_json_view(
            schema=schema_update_image,
            validators=[
                colander_body_validator,
                validate_id,
                validate_image_update,
                validate_associations_update])
    def put(self):
        if not self.request.has_permission('moderator'):
            image_id = self.request.validated['id']
            image = DBSession.query(Image).get(image_id)
            if image is None:
                raise HTTPNotFound('No image found for id {}'.format(image_id))
            if image.image_type == 'collaborative':
                image_type = self.request.validated['document']['image_type']
                if image_type != image.image_type:
                    raise HTTPBadRequest(
                        'Image type cannot be changed for collaborative images'
                    )
            # personal images should only be modifiable by
            # their creator and moderators
            elif not has_been_created_by(image_id,
                                         self.request.authenticated_userid):
                raise HTTPForbidden('No permission to change this image')
        return self._put(Image, schema_image)


# `path` is required by cornice but related routes are not implemented
# because we only need `collection_post` to post a list of images
@resource(collection_path='/images/list', path='/images/list/{id}',
          cors_policy=cors_policy)
class ImageListRest(DocumentRest):

    @restricted_json_view(
            schema=schema_create_image_list,
            validators=[
                colander_body_validator,
                validate_list_image_create,
                validate_list_associations_create])
    def collection_post(self):
        images_in = self.request.validated['images']

        image_ids = []
        images = []
        for document_in in images_in:
            document = create_image(self, document_in)
            images.append(document)
            image_ids.append({'document_id': document.document_id})

        update_feed_images_upload(
            images, images_in, self.request.authenticated_userid)

        return {'images': image_ids}


@resource(path='/images/{id}/{lang}/info', cors_policy=cors_policy)
class ImageInfoRest(DocumentInfoRest):

    @view(validators=[validate_id, validate_lang])
    def get(self):
        return self._get_document_info(Image)


def validate_size(request, **kwargs):
    """Checks if size is one of the available sizes.
    """
    size = request.GET.get('size', None)
    if size in (None, 'SI', 'MI', 'BI'):
        request.validated['size'] = size
    else:
        request.errors.add('querystring', 'size', 'invalid size')


@resource(path='/images/proxy/{id}', cors_policy=cors_policy)
class ImageProxyRest(object):

    def __init__(self, request):
        self.request = request

    @view(validators=[validate_id, validate_size])
    def get(self):
        document_id = self.request.validated['id']
        size = self.request.validated['size']
        query = DBSession. \
            query(Image.filename). \
            filter(Image.document_id == document_id)
        image = query.first()
        if image is None:
            raise HTTPNotFound()
        image_url = self.request.registry.settings['image_url']
        if size is None:
            return HTTPFound("{}{}".format(image_url, image.filename))
        else:
            base, ext = os.path.splitext(image.filename)
            return HTTPFound("{}{}{}{}".format(image_url, base, size, ext))


def set_creator(image):
    """Set the creator (the user who created an image) on an image.
    """
    set_creator_on_documents([image], 'creator')
