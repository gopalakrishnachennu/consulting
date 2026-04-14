from __future__ import annotations

from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin

from core.feature_flags import feature_enabled_for


class FeatureRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    """Return 403 when the current user does not pass feature_enabled_for(feature_key)."""

    feature_key = None

    def test_func(self):
        key = self.feature_key
        if not key:
            return True
        return feature_enabled_for(self.request.user, key)
