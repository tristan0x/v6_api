import colander

from c2corg_api.models import DBSession
from c2corg_api.models.document import DocumentLocale
from c2corg_api.models.document_topic import DocumentTopic
from c2corg_api.security.discourse_client import get_discourse_client

from cornice.resource import resource

from c2corg_api.views import cors_policy, restricted_view

from pyramid.httpexceptions import HTTPInternalServerError


@resource(path='/forum/private-messages/unread-count', cors_policy=cors_policy)
class PrivateMessageRest(object):
    def __init__(self, request):
        self.request = request

    @restricted_view(renderer='json')
    def get(self):
        settings = self.request.registry.settings
        userid = self.request.authenticated_userid

        client = get_discourse_client(settings)
        d_username = client.get_username(userid)
        messages = client.client.private_messages_unread(d_username)

        count = len(messages['topic_list']['topics'])
        link = '%s/users/%s/messages' % (
            client.discourse_public_url, d_username)

        return {link: link, count: count}


class SchemaTopicCreate(colander.MappingSchema):
    document_id = colander.SchemaNode(colander.Int())
    lang = colander.SchemaNode(colander.String())

schema_topic_create = SchemaTopicCreate()


def validate_topic_create(request):
    document_id = request.validated['document_id']
    lang = request.validated['lang']

    locale = DBSession.query(DocumentLocale) \
        .filter(DocumentLocale.document_id == document_id) \
        .filter(DocumentLocale.lang == lang) \
        .one_or_none()
    if locale is None:
        request.errors.add('body',
                           '{}/{}'.format(document_id, lang),
                           'Document not found')
        return
    request.validated['locale'] = locale

    if locale.topic_id is not None:
        request.errors.add('body',
                           '{}_{}'.format(document_id, lang),
                           'Topic already exists')


# Here path is required by cornice but related routes are not implemented
# as far as we only need collection_post to create topic in discourse
@resource(collection_path='/forum/topics', path='/forum/topics/{id}',
          cors_policy=cors_policy)
class ForumTopicRest(object):
    def __init__(self, request):
        self.request = request

    @restricted_view(schema=schema_topic_create,
                     validators=[validate_topic_create])
    def collection_post(self):
        settings = self.request.registry.settings

        locale = self.request.validated['locale']

        title = "{}_{}".format(locale.document_id, locale.lang)
        content = '<a href="{}">{}</a>'.format(
                self.request.referer,
                locale.title)
        category = settings['discourse.category']
        # category could be id or name
        try:
            category = int(category)
        except:
            pass

        client = get_discourse_client(settings)
        try:
            response = client.client.create_post(content,
                                                 title=title,
                                                 category=category)
        except:
            raise HTTPInternalServerError('Error with Discourse')

        if "topic_id" in response:
            document_topic = DocumentTopic(topic_id=response['topic_id'])
            locale.document_topic = document_topic
            DBSession.flush()

        return response
