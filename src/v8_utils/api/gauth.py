"""The operator's Google user credential, from the gcloud ADC file.

For an in-process consumer that builds its own Google client (a Chat app
impersonating its service account, a Workspace Events session) and needs the
same source policy as v8-utils' own identity module: the gcloud login file,
never GOOGLE_APPLICATION_CREDENTIALS, and never a service-account key.
"""

from ..gauth import LOGIN_HINT, adc_path, credentials

__all__ = ["LOGIN_HINT", "adc_path", "credentials"]
