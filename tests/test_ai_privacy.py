from io import BytesIO

from PIL import Image

from ai_service import AI_IMAGE_MAX_BYTES, prepare_ai_photo, sanitize_description


def test_sanitize_description_masks_phone_and_email() -> None:
    description = "Позвоните +7 (999) 123-45-67 или напишите user@example.com"

    assert sanitize_description(description) == "Позвоните [телефон скрыт] или напишите [email скрыт]"


def test_prepare_ai_photo_removes_metadata_and_limits_size() -> None:
    image = Image.new("RGB", (2400, 1800), "blue")
    source = BytesIO()
    image.save(source, format="JPEG")

    prepared = prepare_ai_photo(source.getvalue())
    assert len(prepared) <= AI_IMAGE_MAX_BYTES
    with Image.open(BytesIO(prepared)) as result:
        assert max(result.size) <= 1600
        assert not result.getexif()
