"""How HubSpot handles Dialpad activity (the framework only delivers events):

- ``calls.py`` (``hubspot.calls``): fold call events, then log one HubSpot call per Dialpad call
- ``sms.py`` (``hubspot.sms``): log one HubSpot communication per SMS
- ``notes.py`` (``hubspot.notes``): add contact-card notes to the logged call
- ``fold.py``: the order-independent fold of call events (pure functions)
- ``settings.py``: ``HUBSPOT_CALLS_*`` / ``HUBSPOT_SMS_*``, what gets logged
"""
