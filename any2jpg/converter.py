"""Conversione di immagini in JPEG: logica pura, senza dipendenze dalla GUI."""

from __future__ import annotations

import os
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from PIL import Image, ImageCms, ImageOps

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_SUPPORTED = True
except ImportError:
    HEIF_SUPPORTED = False

JPEG_EXTENSIONS = frozenset({".jpg", ".jpeg", ".jpe", ".jfif"})
WHITE = (255, 255, 255)

TAG_ARTIST = 0x013B
TAG_COPYRIGHT = 0x8298
TAG_ORIENTATION = 0x0112

_ALPHA_MODES = frozenset({"RGBA", "RGBa", "LA", "La", "PA"})
_ICC_SPACE_FOR_MODE = {"RGB": "RGB ", "L": "GRAY"}


@dataclass(frozen=True)
class MetadataOptions:
    """Cosa fare dei metadati.

    keep_original=True copia EXIF/XMP dell'originale, False li elimina tutti.
    author/copyright, se non vuoti, vengono scritti in ogni caso (EXIF + XMP).
    """

    keep_original: bool = True
    author: str = ""
    copyright: str = ""

    @property
    def overrides(self) -> bool:
        return bool(self.author or self.copyright)


def supported_extensions() -> frozenset[str]:
    """Estensioni (minuscole, con punto) che Pillow sa aprire."""
    return frozenset(
        ext.lower()
        for ext, fmt in Image.registered_extensions().items()
        if fmt in Image.OPEN
    )


def scan_folder(folder: Path, recursive: bool, include_jpeg: bool = False) -> list[Path]:
    """Elenca le immagini convertibili in una cartella, saltando file e cartelle nascosti."""
    extensions = supported_extensions()
    if not include_jpeg:
        extensions -= JPEG_EXTENSIONS
    candidates = folder.rglob("*") if recursive else folder.glob("*")
    return sorted(
        path
        for path in candidates
        if path.suffix.lower() in extensions
        and path.is_file()
        and not any(part.startswith(".") for part in path.relative_to(folder).parts)
    )


def plan_destination(
    source: Path,
    root: Path | None,
    output_dir: Path | None,
    overwrite: bool,
    reserved: set[Path],
) -> Path:
    """Sceglie il percorso del JPG di uscita.

    Senza output_dir il file finisce accanto all'originale. Con output_dir, i file
    provenienti da una cartella (root) mantengono la struttura delle sottocartelle.
    Non restituisce mai il file sorgente né un percorso già assegnato in questo lotto
    (reserved); senza overwrite evita anche i file esistenti aggiungendo _1, _2, ...
    """
    source = source.resolve()
    if output_dir is None:
        target_dir = source.parent
    elif root is not None:
        target_dir = output_dir.resolve() / source.parent.relative_to(root.resolve())
    else:
        target_dir = output_dir.resolve()

    candidate = target_dir / f"{source.stem}.jpg"
    counter = 1
    while (
        candidate == source
        or candidate in reserved
        or (not overwrite and candidate.exists())
    ):
        candidate = target_dir / f"{source.stem}_{counter}.jpg"
        counter += 1
    return candidate


def convert_file(
    source: Path,
    destination: Path,
    quality: int,
    metadata: MetadataOptions,
    background: tuple[int, int, int] = WHITE,
) -> None:
    """Converte source in un JPEG scritto in destination (scrittura atomica)."""
    with Image.open(source) as original:
        # Ruota i pixel secondo l'orientamento EXIF e rimuove il tag.
        image = ImageOps.exif_transpose(original)
        exif, xmp = _build_metadata(image, metadata)
        icc = image.info.get("icc_profile")
        dpi = image.info.get("dpi")
        image, icc = _to_jpeg_mode(image, icc, background)

    params: dict = {"quality": quality, "optimize": True}
    if exif:
        params["exif"] = exif
    if xmp:
        params["xmp"] = xmp
    if icc:
        params["icc_profile"] = icc
    if dpi:
        params["dpi"] = dpi

    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f".{destination.name}.{os.getpid()}.part")
    try:
        with open(partial, "xb") as handle:
            image.save(handle, "JPEG", **params)
        os.replace(partial, destination)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def _build_metadata(image: Image.Image, options: MetadataOptions) -> tuple[bytes | None, bytes | None]:
    exif = image.getexif() if options.keep_original else Image.Exif()
    if TAG_ORIENTATION in exif:
        # I pixel sono già raddrizzati: un orientamento residuo li ruoterebbe di nuovo.
        exif[TAG_ORIENTATION] = 1
    # Byte UTF-8: con le str Pillow sostituirebbe "©" e le lettere accentate con "?".
    if options.author:
        exif[TAG_ARTIST] = options.author.encode("utf-8")
    if options.copyright:
        exif[TAG_COPYRIGHT] = options.copyright.encode("utf-8")
    exif_bytes = exif.tobytes() if len(exif) else None

    if options.overrides:
        xmp = _xmp_packet(options.author, options.copyright)
    elif options.keep_original:
        xmp = image.info.get("xmp") or image.info.get("XML:com.adobe.xmp")
        if isinstance(xmp, str):
            xmp = xmp.encode("utf-8")
    else:
        xmp = None
    return exif_bytes, xmp


def _xmp_packet(author: str, copyright_: str) -> bytes:
    fields = []
    if author:
        fields.append(f"<dc:creator><rdf:Seq><rdf:li>{escape(author)}</rdf:li></rdf:Seq></dc:creator>")
    if copyright_:
        fields.append(
            "<dc:rights><rdf:Alt>"
            f'<rdf:li xml:lang="x-default">{escape(copyright_)}</rdf:li>'
            "</rdf:Alt></dc:rights>"
        )
    return (
        '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/">'
        + "".join(fields)
        + "</rdf:Description></rdf:RDF></x:xmpmeta>"
        '<?xpacket end="w"?>'
    ).encode("utf-8")


def _to_jpeg_mode(
    image: Image.Image, icc: bytes | None, background: tuple[int, int, int]
) -> tuple[Image.Image, bytes | None]:
    """Porta l'immagine in RGB o L, appiattendo la trasparenza su background."""
    if image.mode == "P":
        image = image.convert("RGBA" if "transparency" in image.info else "RGB")
    elif image.mode == "1":
        image = image.convert("L")
    elif image.mode.startswith("I") or image.mode == "F":
        image = _to_8bit_gray(image)
    elif image.mode == "CMYK":
        image, icc = _cmyk_to_srgb(image, icc)

    if image.mode in _ALPHA_MODES:
        rgba = image.convert("RGBA")
        flat = Image.new("RGB", rgba.size, background)
        flat.paste(rgba, mask=rgba.getchannel("A"))
        image = flat
    elif image.mode not in ("RGB", "L"):
        image = image.convert("RGB")

    if icc and not _icc_matches(icc, image.mode):
        icc = None
    return image, icc


def _to_8bit_gray(image: Image.Image) -> Image.Image:
    if image.mode != "F":
        image = image.convert("I")
    low, high = image.getextrema()
    if image.mode == "F" and high <= 1.0:
        scale = 255.0
    elif high > 255:
        scale = 1 / 256
    else:
        scale = 1.0
    return image.point(lambda value: value * scale).convert("L")


def _cmyk_to_srgb(image: Image.Image, icc: bytes | None) -> tuple[Image.Image, bytes | None]:
    srgb = ImageCms.createProfile("sRGB")
    if icc:
        try:
            converted = ImageCms.profileToProfile(
                image, ImageCms.ImageCmsProfile(BytesIO(icc)), srgb, outputMode="RGB"
            )
            return converted, ImageCms.ImageCmsProfile(srgb).tobytes()
        except (ImageCms.PyCMSError, OSError):
            pass
    return image.convert("RGB"), None


def _icc_matches(icc: bytes, mode: str) -> bool:
    try:
        space = ImageCms.ImageCmsProfile(BytesIO(icc)).profile.xcolor_space
    except (ImageCms.PyCMSError, OSError):
        return False
    return _ICC_SPACE_FOR_MODE.get(mode) == space
