from functools import lru_cache
from ra_utils.job_settings import JobSettings


class EngagementSettings(JobSettings):
    # common settings for clients:
    pass


@lru_cache()
def get_engagement_settings(*args, **kwargs) -> EngagementSettings:
    return EngagementSettings(*args, **kwargs)
