"""Local puzzle solver adapted from ha-ep-cube (MIT).

Copyright (c) 2026 Michael Skilton. See THIRD_PARTY_NOTICES.md.
Only image matching/encryption is reused; no Home Assistant/network code.
"""

import base64
import json
from io import BytesIO

import numpy as np
from PIL import Image

_CAPTCHA_REPORT_Y = 5


def _solve_captcha_image(original_b64: str, puzzle_b64: str) -> int | None:
    """Decode both images and return the x-pixel where the puzzle piece sits.

    Returns None if the captcha images have dimensions outside what the
    matcher can handle (puzzle larger than background in either axis after
    the small-delta height pad). The caller should retry with a fresh
    captcha in that case."""
    background = _decode_b64_image_grey(original_b64)
    puzzle = _decode_b64_image_grey(puzzle_b64)

    # ~40% of captchas come back with bg 2 px shorter than the puzzle in
    # height (renderer drops the slider track). Pad bg with its mean value
    # to match — the added rows align with the puzzle's transparent
    # (rendered as 0 after decode) regions, so accuracy is preserved.
    height_delta = puzzle.shape[0] - background.shape[0]
    if 0 < height_delta <= 4:
        pad_top = height_delta // 2
        pad_bot = height_delta - pad_top
        mean_val = int(background.mean())
        background = np.pad(
            background,
            ((pad_top, pad_bot), (0, 0)),
            mode="constant",
            constant_values=mean_val,
        )

    if puzzle.shape[0] > background.shape[0] or puzzle.shape[1] > background.shape[1]:
        return None

    res = _match_template_ccoeff_normed(background, puzzle)
    flat = int(res.argmax())
    y, x = np.unravel_index(flat, res.shape)
    return int(x)


def _match_template_ccoeff_normed(image: np.ndarray, template: np.ndarray) -> np.ndarray:
    """Pure-numpy equivalent of cv2.matchTemplate(..., TM_CCOEFF_NORMED).

    Returns a 2D float64 of shape (H-h+1, W-w+1); entry [y,x] is the
    normalized cross-correlation when template's top-left sits at (y,x)
    in the image. Two tricks:
      1. Integral images give per-window sum + sum-of-squares in O(1).
      2. Zero-mean template makes the numerator a plain cross-correlation,
         which FFT computes in O(N log N) rather than O(N·n).
    """
    image = image.astype(np.float64)
    template = template.astype(np.float64)
    H, W = image.shape
    h, w = template.shape
    n = h * w

    t_zm = template - template.mean()
    t_norm_sq = float((t_zm**2).sum())

    integral = np.zeros((H + 1, W + 1), dtype=np.float64)
    integral[1:, 1:] = image.cumsum(axis=0).cumsum(axis=1)
    integral_sq = np.zeros((H + 1, W + 1), dtype=np.float64)
    integral_sq[1:, 1:] = (image**2).cumsum(axis=0).cumsum(axis=1)

    win_sum = integral[h:, w:] - integral[:-h, w:] - integral[h:, :-w] + integral[:-h, :-w]
    win_sq = (
        integral_sq[h:, w:] - integral_sq[:-h, w:] - integral_sq[h:, :-w] + integral_sq[:-h, :-w]
    )
    win_var_n = np.maximum(win_sq - (win_sum**2) / n, 0.0)

    fft_shape = (H + h - 1, W + w - 1)
    F_I = np.fft.rfft2(image, s=fft_shape)
    F_T = np.fft.rfft2(t_zm[::-1, ::-1], s=fft_shape)
    corr_full = np.fft.irfft2(F_I * F_T, s=fft_shape)
    numerator = corr_full[h - 1 : H, w - 1 : W]

    denom = np.sqrt(t_norm_sq * win_var_n)
    return np.where(denom > 1e-10, numerator / denom, 0.0)


def _decode_b64_image_grey(b64: str) -> np.ndarray:
    """Decode a base64 PNG to a 2D uint8 greyscale numpy array.

    The EP Cube background image is encoded in PIL "P" (palette) mode.
    `np.array(palette_image)` returns the raw palette indices, NOT the
    looked-up RGB values, and that index space is what cross-correlates
    with the puzzle's signal. Doing the textbook-correct `convert("L")`
    destroys the match (mean luminance ~142, scores ~0.2 → 0% acceptance);
    leaving palette indices alone gives mean ~44 and 80% acceptance. See
    docs/PHASE_3_2.md spike notes.

    RGBA puzzles use OpenCV's BGR2GRAY weight order: we apply
    gray = 0.114R + 0.587G + 0.299B (note R/B are swapped vs the standard
    ITU-R 601 weights — this is a deliberate cv2 quirk we mirror so scores
    stay bit-exact with the cube's expected match path)."""
    raw = base64.b64decode(b64)
    pil = Image.open(BytesIO(raw))
    arr = np.array(pil)
    if arr.ndim == 2:
        return arr.astype(np.uint8)
    if arr.shape[2] == 4:
        rgb = arr[..., :3].astype(np.float32)
        gray = 0.114 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.299 * rgb[..., 2]
        return gray.astype(np.uint8)
    if arr.shape[2] == 3:
        rgb = arr.astype(np.float32)
        gray = 0.114 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.299 * rgb[..., 2]
        return gray.astype(np.uint8)
    raise ValueError(f"Unexpected captcha image shape: {arr.shape}")


def _encrypt_point(x: float, y: float, secret_key: str) -> str:
    """AES-ECB(secret_key) of `{"x":<x>,"y":<y>}` JSON, base64-encoded.
    This is the pointJson sent to /captcha/check.

    `pycryptodome` is imported lazily so a missing dep only blows up when
    a login is actually attempted (not on module import / passive sensor
    polling)."""
    body = json.dumps({"x": float(x), "y": float(y)}, separators=(",", ":")).encode("utf-8")
    return _aes_ecb_base64(body, secret_key)


def _build_captcha_verification(token: str, x: float, y: float, secret_key: str) -> str:
    """Construct the `captchaVerification` string passed to /login.

    Crucially this is NOT `token + "---" + pointJson` (that was my first
    incorrect guess and the cube responds "captcha is overdue"). The real
    format is:
        AES_ECB(secret_key, f"{token}---{json({x,y})}")  base64-encoded
    i.e. AES-encrypt the whole literal `token---plaintext_json` string,
    not just the {x,y} point. Same `secret_key` as used for pointJson.
    Confirmed against the live cube 2026-05-21."""
    json_point = json.dumps({"x": float(x), "y": float(y)}, separators=(",", ":"))
    raw = f"{token}---{json_point}".encode()
    return _aes_ecb_base64(raw, secret_key)


def _aes_ecb_base64(plaintext: bytes, key: str) -> str:
    """AES-ECB encrypt with PKCS7 padding, base64-encoded output."""
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad

    cipher = AES.new(key.encode("utf-8"), AES.MODE_ECB)
    return base64.b64encode(cipher.encrypt(pad(plaintext, AES.block_size))).decode("utf-8")


# ----------------------------------------------------------------------
