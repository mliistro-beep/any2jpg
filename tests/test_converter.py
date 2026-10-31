import os
from pathlib import Path

import pytest
from PIL import Image, ImageCms

from any2jpg.converter import (
    HEIF_SUPPORTED,
    TAG_ARTIST,
    TAG_COPYRIGHT,
    TAG_ORIENTATION,
    MetadataOptions,
    convert_file,
    make_preview,
    plan_destination,
    scan_folder,
    supported_extensions,
)

TAG_MAKE = 0x010F


def _convert(tmp_path: Path, image: Image.Image, name: str, metadata=MetadataOptions(), **save) -> Image.Image:
    source = tmp_path / name
    image.save(source, **save)
    destination = tmp_path / "out.jpg"
    convert_file(source, destination, 90, metadata)
    result = Image.open(destination)
    assert result.format == "JPEG"
    return result


def test_transparent_png_is_flattened_on_white(tmp_path):
    image = Image.new("RGBA", (8, 8), (255, 0, 0, 0))
    result = _convert(tmp_path, image, "a.png")
    assert result.mode == "RGB"
    assert result.getpixel((4, 4)) == pytest.approx((255, 255, 255), abs=2)


def test_palette_with_transparency(tmp_path):
    image = Image.new("P", (8, 8), 0)
    image.putpalette([0, 0, 0] * 256)
    result = _convert(tmp_path, image, "p.gif", transparency=0)
    assert result.getpixel((0, 0)) == pytest.approx((255, 255, 255), abs=2)


def test_16bit_grayscale_keeps_tones(tmp_path):
    image = Image.new("I;16", (8, 8), 32768)
    result = _convert(tmp_path, image, "g.png")
    assert result.mode == "L"
    assert result.getpixel((0, 0)) == pytest.approx(128, abs=2)


def test_cmyk_tiff(tmp_path):
    image = Image.new("CMYK", (8, 8), (0, 255, 255, 0))
    result = _convert(tmp_path, image, "c.tif")
    assert result.mode == "RGB"
    red, green, blue = result.getpixel((4, 4))
    assert red > 200 and green < 60 and blue < 60


def test_rgb_icc_profile_is_kept(tmp_path):
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    result = _convert(tmp_path, Image.new("RGB", (8, 8)), "i.png", icc_profile=icc)
    assert result.info.get("icc_profile") == icc


def test_exif_orientation_is_applied(tmp_path):
    exif = Image.Exif()
    exif[TAG_ORIENTATION] = 6  # ruota di 90° in senso orario
    result = _convert(tmp_path, Image.new("RGB", (20, 10)), "o.jpg", exif=exif)
    assert result.size == (10, 20)
    assert result.getexif().get(TAG_ORIENTATION, 1) == 1


def _exif_with_make() -> Image.Exif:
    exif = Image.Exif()
    exif[TAG_MAKE] = "Fotocamera"
    return exif


def test_keep_original_metadata(tmp_path):
    result = _convert(tmp_path, Image.new("RGB", (8, 8)), "m.png", exif=_exif_with_make())
    assert result.getexif()[TAG_MAKE] == "Fotocamera"


def test_strip_all_metadata(tmp_path):
    result = _convert(
        tmp_path, Image.new("RGB", (8, 8)), "m.png",
        MetadataOptions(keep_original=False), exif=_exif_with_make(),
    )
    assert len(result.getexif()) == 0
    assert "xmp" not in result.info


def test_strip_but_set_author_and_copyright_utf8(tmp_path):
    metadata = MetadataOptions(keep_original=False, author="Niccolò", copyright="© 2026 Niccolò")
    result = _convert(tmp_path, Image.new("RGB", (8, 8)), "m.png", metadata, exif=_exif_with_make())
    exif = result.getexif()
    assert TAG_MAKE not in exif
    raw = result.info["exif"]
    assert "Niccolò".encode() in raw and "© 2026 Niccolò".encode() in raw
    assert set(exif) == {TAG_ARTIST, TAG_COPYRIGHT}
    xmp = result.info["xmp"].decode()
    assert "<rdf:li>Niccolò</rdf:li>" in xmp and "© 2026 Niccolò" in xmp


def test_keep_and_override_author(tmp_path):
    result = _convert(
        tmp_path, Image.new("RGB", (8, 8)), "m.png",
        MetadataOptions(author="A & B"), exif=_exif_with_make(),
    )
    exif = result.getexif()
    assert exif[TAG_MAKE] == "Fotocamera"
    assert exif[TAG_ARTIST] == "A & B"
    assert TAG_COPYRIGHT not in exif
    assert "A &amp; B" in result.info["xmp"].decode()


def test_avif_source(tmp_path):
    result = _convert(tmp_path, Image.new("RGB", (16, 16), (0, 0, 255)), "x.avif")
    assert result.getpixel((8, 8))[2] > 200


@pytest.mark.skipif(not HEIF_SUPPORTED, reason="pillow-heif non installato")
def test_heic_source(tmp_path):
    assert ".heic" in supported_extensions()
    result = _convert(tmp_path, Image.new("RGB", (16, 16), (0, 255, 0)), "x.heic")
    assert result.getpixel((8, 8))[1] > 200


def test_destination_next_to_source_by_default(tmp_path):
    source = tmp_path / "foto.png"
    assert plan_destination(source, None, None, False, set()) == tmp_path / "foto.jpg"


def test_destination_avoids_existing_and_reserved(tmp_path):
    (tmp_path / "foto.jpg").touch()
    source = tmp_path / "foto.png"
    first = plan_destination(source, None, None, False, set())
    assert first == tmp_path / "foto_1.jpg"
    second = plan_destination(tmp_path / "foto.webp", None, None, False, {first})
    assert second == tmp_path / "foto_2.jpg"


def test_destination_overwrite_but_never_source(tmp_path):
    (tmp_path / "foto.jpg").touch()
    assert plan_destination(tmp_path / "foto.png", None, None, True, set()) == tmp_path / "foto.jpg"
    assert plan_destination(tmp_path / "foto.jpg", None, None, True, set()) == tmp_path / "foto_1.jpg"


def test_destination_mirrors_subfolders_in_output_dir(tmp_path):
    root = tmp_path / "album"
    out = tmp_path / "export"
    source = root / "2024" / "estate" / "mare.png"
    assert plan_destination(source, root, out, False, set()) == out / "2024" / "estate" / "mare.jpg"
    assert plan_destination(source, None, out, False, set()) == out / "mare.jpg"


def test_scan_folder(tmp_path):
    for relative in ["a.png", "b.JPG", "note.txt", "sub/c.webp", ".hidden/d.png", "sub/.e.png"]:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    assert scan_folder(tmp_path, recursive=False) == [tmp_path / "a.png"]
    assert scan_folder(tmp_path, recursive=True) == [tmp_path / "a.png", tmp_path / "sub" / "c.webp"]
    assert tmp_path / "b.JPG" in scan_folder(tmp_path, recursive=False, include_jpeg=True)


def test_max_side_shrinks_keeping_ratio(tmp_path):
    source = tmp_path / "grande.png"
    Image.new("RGB", (400, 100)).save(source)
    convert_file(source, tmp_path / "out.jpg", 90, MetadataOptions(), max_side=200)
    assert Image.open(tmp_path / "out.jpg").size == (200, 50)


def test_max_side_never_enlarges(tmp_path):
    source = tmp_path / "piccola.png"
    Image.new("RGB", (40, 10)).save(source)
    convert_file(source, tmp_path / "out.jpg", 90, MetadataOptions(), max_side=200)
    assert Image.open(tmp_path / "out.jpg").size == (40, 10)


def test_max_side_applies_after_exif_rotation(tmp_path):
    exif = Image.Exif()
    exif[TAG_ORIENTATION] = 6
    source = tmp_path / "ruotata.jpg"
    Image.new("RGB", (400, 100)).save(source, exif=exif)
    convert_file(source, tmp_path / "out.jpg", 90, MetadataOptions(), max_side=200)
    assert Image.open(tmp_path / "out.jpg").size == (50, 200)


def test_keep_mtime(tmp_path):
    source = tmp_path / "vecchia.png"
    Image.new("RGB", (8, 8)).save(source)
    old = 1_600_000_000_000_000_000  # settembre 2020, in nanosecondi
    os.utime(source, ns=(old, old))
    convert_file(source, tmp_path / "a.jpg", 90, MetadataOptions(), keep_mtime=True)
    convert_file(source, tmp_path / "b.jpg", 90, MetadataOptions())
    assert (tmp_path / "a.jpg").stat().st_mtime_ns == old
    assert (tmp_path / "b.jpg").stat().st_mtime_ns > old


def test_preview_fits_and_reports_original_size(tmp_path):
    source = tmp_path / "p.png"
    Image.new("RGBA", (400, 200), (0, 0, 255, 0)).save(source)
    preview = make_preview(source, 100)
    assert preview.image.size == (100, 50)
    assert preview.image.mode == "RGB"
    assert (preview.width, preview.height, preview.format) == (400, 200, "PNG")


def test_preview_of_rotated_jpeg(tmp_path):
    exif = Image.Exif()
    exif[TAG_ORIENTATION] = 6
    source = tmp_path / "r.jpg"
    Image.new("RGB", (400, 200)).save(source, exif=exif)
    preview = make_preview(source, 100)
    assert preview.image.size == (50, 100)
    assert (preview.width, preview.height) == (200, 400)


def test_preview_of_16bit_image(tmp_path):
    source = tmp_path / "g.png"
    Image.new("I;16", (64, 64), 65535).save(source)
    preview = make_preview(source, 32)
    assert preview.image.mode == "L"
    assert preview.image.getpixel((0, 0)) == pytest.approx(255, abs=2)


def test_failed_conversion_leaves_no_partial_file(tmp_path):
    source = tmp_path / "rotto.png"
    source.write_bytes(b"non sono un png")
    with pytest.raises(Exception):
        convert_file(source, tmp_path / "rotto.jpg", 90, MetadataOptions())
    assert sorted(p.name for p in tmp_path.iterdir()) == ["rotto.png"]
