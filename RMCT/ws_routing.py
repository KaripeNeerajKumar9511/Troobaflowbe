from django.urls import re_path

from apps.rmct.consumers import ModelCollaborationConsumer, OrganizationCollaborationConsumer


websocket_urlpatterns = [
    re_path(r"^ws/org/$", OrganizationCollaborationConsumer.as_asgi()),
    re_path(r"^ws/models/(?P<model_id>[0-9a-f-]+)/$", ModelCollaborationConsumer.as_asgi()),
]

