"""Meta WhatsApp Cloud API channel plugin.

A ``tai42_contract.channels.Channel`` that delivers ``ask_user`` questions over
the WhatsApp Cloud (Meta Graph) API and bridges replies back through its own
webhook route. Importing this package does NOT register anything (library use);
the runtime imports ``tai42_channel_whatsapp_cloud.register`` to register the
``"whatsapp-cloud"`` channel and its inbound route.
"""

from tai42_channel_whatsapp_cloud.channel import WhatsAppCloudChannel
from tai42_channel_whatsapp_cloud.settings import WhatsAppCloudSettings, whatsapp_cloud_settings

__all__ = ["WhatsAppCloudChannel", "WhatsAppCloudSettings", "whatsapp_cloud_settings"]
