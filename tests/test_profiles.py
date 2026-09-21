from sqzit.profiles import PROFILES, estimate_factor, get_profile


def test_profiles_are_available() -> None:
    assert set(PROFILES) == {"balanced", "smaller", "lossless"}
    assert get_profile("balanced").video_codec == "libx264"


def test_lossless_profile_does_not_claim_large_image_savings() -> None:
    assert estimate_factor("image", get_profile("lossless"), "jpeg") >= 1


def test_smaller_profile_estimates_savings() -> None:
    profile = get_profile("smaller")
    assert estimate_factor("video", profile, "h264") < 1
