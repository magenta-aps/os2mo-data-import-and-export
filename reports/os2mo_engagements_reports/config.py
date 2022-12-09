from functools import lru_cache
from typing import Any
from typing import cast
from typing import Dict
from typing import List

from pydantic import AnyHttpUrl
from pydantic import BaseSettings

from ra_utils.apply import apply
from ra_utils.job_settings import JobSettings


class EngagementSettings(JobSettings):
    # common settings for clients:
    pass


@lru_cache()
def get_engagement_settings(*args, **kwargs) -> EngagementSettings:  # WHAT SHOULD IT BE CALLED???
    return EngagementSettings(*args, **kwargs)
