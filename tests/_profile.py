"""Pouyan's customer profile for tests that build a Config directly (the core itself has no customer defaults)."""
import os

from agent.profile import load_profile

PROFILE = load_profile(os.path.join(os.path.dirname(__file__), "..", "customers", "pouyan", "profile.yaml"))
import yaml

_CFG = yaml.safe_load(open(os.path.join(os.path.dirname(__file__), "..", "customers", "pouyan", "config.yaml"), encoding="utf-8"))
P = dict(profile=PROFILE, channel_name=PROFILE.name, exclude_words=_CFG["exclude_words"], exclude_ignore=_CFG["exclude_ignore"])
