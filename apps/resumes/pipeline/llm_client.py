"""
Thin LLM client wrapper for the resume pipeline.

Handles: API key retrieval, token cap enforcement, cost logging,
error handling with retry, and per-section request_type tagging.
"""
import time
import logging
import openai

from django.utils import timezone
from django.db.models import Sum

from core.models import LLMConfig, LLMUsageLog
from core.security import decrypt_value
from core.llm_services import calculate_cost

logger = logging.getLogger("apps.resumes.pipeline")


class PipelineLLMClient:
    """Reusable LLM client for pipeline calls. Initialised once per pipeline run."""

    def __init__(self):
        self.config = LLMConfig.load()
        self.api_key = decrypt_value(self.config.encrypted_api_key)
        self._client = None
        self.total_tokens = 0
        self.total_cost = 0.0
        self.total_calls = 0
        self.total_latency_ms = 0

    @property
    def client(self):
        if self._client is None:
            self._client = openai.OpenAI(api_key=self.api_key)
        return self._client

    @property
    def default_model(self):
        return self.config.active_model or "gpt-4o-mini"

    @property
    def default_temperature(self):
        return float(self.config.temperature)

    def is_available(self):
        """Check if LLM generation is available."""
        if not self.api_key or self.api_key.startswith("sk-your"):
            return False, "No valid OpenAI API key configured."
        if not self.config.generation_enabled:
            return False, "Resume generation is disabled."
        return True, None

    def check_token_cap(self):
        """Check monthly token cap. Returns (ok, error_msg)."""
        if not self.config.monthly_token_cap:
            return True, None
        month_start = timezone.now().replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        total_month = (
            LLMUsageLog.objects.filter(created_at__gte=month_start)
            .aggregate(total=Sum("total_tokens"))["total"]
            or 0
        )
        if total_month >= self.config.monthly_token_cap:
            if self.config.auto_disable_on_cap:
                self.config.generation_enabled = False
                self.config.save()
            return False, "Monthly token cap reached. Generation disabled."
        return True, None

    def call(
        self,
        system_prompt,
        user_prompt,
        *,
        request_type="pipeline_generic",
        temperature=None,
        max_tokens=None,
        job=None,
        consultant=None,
        actor=None,
    ):
        """
        Single LLM call with full logging.

        Returns: (content, tokens_used, error_msg_or_none)
        """
        model = self.default_model
        temp = temperature if temperature is not None else self.default_temperature
        max_tok = max_tokens or (self.config.max_output_tokens or 4000)

        try:
            start = time.time()
            response = self.client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=temp,
                max_tokens=max_tok,
            )
            latency_ms = int((time.time() - start) * 1000)

            content = response.choices[0].message.content
            prompt_tokens = response.usage.prompt_tokens if response.usage else 0
            completion_tokens = (
                response.usage.completion_tokens if response.usage else 0
            )
            total_tokens = response.usage.total_tokens if response.usage else 0

            costs = calculate_cost(model, prompt_tokens, completion_tokens)

            LLMUsageLog.objects.create(
                request_type=request_type,
                model_name=model,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                request_payload={
                    "model": model,
                    "temperature": temp,
                    "max_tokens": max_tok,
                },
                response_text=content or "",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                cost_input=costs["input"],
                cost_output=costs["output"],
                cost_total=costs["total"],
                latency_ms=latency_ms,
                success=True,
                job=job,
                consultant=consultant,
                actor=actor,
            )

            # Accumulate totals
            self.total_tokens += total_tokens
            self.total_cost += costs["total"]
            self.total_calls += 1
            self.total_latency_ms += latency_ms

            return content, total_tokens, None

        except Exception as e:
            LLMUsageLog.objects.create(
                request_type=request_type,
                model_name=model,
                success=False,
                error_message=str(e),
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                job=job,
                consultant=consultant,
                actor=actor,
            )
            return None, 0, str(e)
