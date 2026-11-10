"""fal's post-trained H3 Max; deliberately distinct from MiniMax H3."""

from tools.fal_media import FalMedia


class H3MaxVideo(FalMedia):
    name = "h3_max_video"
    capability = "video_generation"
    capabilities = ["text_to_video", "image_to_video", "reference_to_video"]
    supports = dict.fromkeys(
        ["text_to_video", "image_to_video", "reference_to_video", "native_audio"], True
    )
    best_for = ["Fast H3 Max drafts and final clips with reference control"]
    routes = {
        "h3-max": {op: f"minimax/h3-max/{op.replace('_', '-')}" for op in capabilities}
    }
    # fal pricing API, 2026-11-07 - BASE (480P) rates. Real billing events (2026-11-09)
    # showed 768P bills 1.6x the base ($0.056/s for image-to-video), which the pricing
    # API does not surface; _billed_rate applies it, and settlement reconciles against
    # billing events regardless (lib/fal_billing.py).
    unit_prices = {
        "minimax/h3-max/image-to-video": 0.035,
        "minimax/h3-max/text-to-video": 0.035,
        "minimax/h3-max/reference-to-video": 0.05,
    }
    _HI_RES_MULTIPLIER = 1.6

    def _billed_rate(self, endpoint, inputs):
        rate = self.unit_prices.get(endpoint)
        if rate is None:
            return None
        resolution = str((inputs or {}).get("resolution") or "768P").upper()
        return round(rate * (1.0 if resolution == "480P" else self._HI_RES_MULTIPLIER), 4)

    def _billed_seconds(self, inputs, endpoint):
        """fal bills an H3 Max clip for the length it was asked for, in whole seconds, not for
        the file it delivers: all four 5-second clips of videos 713 and 714 were billed 5
        seconds, though each file runs 5.18 s. Counting the file (or the overrun a file may
        have) priced them at 6."""
        import math

        return math.ceil(float(self._requested_seconds(inputs, endpoint)))

    def estimate_cost(self, inputs):
        endpoint = self._route(inputs)[2]
        rate = self._billed_rate(endpoint, inputs)
        if rate is None:
            return super().estimate_cost(inputs)  # keeps the quote-required refusal
        return round(rate * self._billed_seconds(inputs, endpoint), 4)

    def _output_cost(self, endpoint, output, inputs):
        rate = self._billed_rate(endpoint, inputs)
        if rate is None:
            return None
        return round(rate * self._billed_seconds(inputs, endpoint), 4)
