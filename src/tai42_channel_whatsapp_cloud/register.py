"""Registration side-effects: the ``"whatsapp-cloud"`` channel and its inbound route.

Loaded when the manifest's ``channel_modules`` lists ``tai42_channel_whatsapp_cloud``.
Importing THIS module registers the channel on ``tai42_app.channels`` and (via the
``inbound`` import) the unauthenticated webhook route; importing the package
``__init__`` alone does NOT (library use).

The WhatsApp number's webhook is configured OUT-OF-BAND (Meta App dashboard):
point the webhook callback URL at ``{public base URL}/api/channels/whatsapp-cloud/inbound``
and set the verify token to ``CHANNEL_WHATSAPP_CLOUD_VERIFY_TOKEN``. The plugin
never mutates Meta app configuration.
"""

from tai42_contract.app import tai42_app

import tai42_channel_whatsapp_cloud.inbound  # noqa: F401  (route registration side-effect)
from tai42_channel_whatsapp_cloud.channel import WhatsAppCloudChannel

tai42_app.channels.register("whatsapp-cloud", WhatsAppCloudChannel())
