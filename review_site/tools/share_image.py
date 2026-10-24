"""Make the site's title-page pictures from the comic's title page: the link preview (what iMessage, WhatsApp, Slack,
LinkedIn... show when the address is shared), the home-screen icon, and the page background. Writes small files into the
site's static folder; commit them and redeploy.

    python -m review_site.tools.share_image                      # uses runs/book/title/page.png
    python -m review_site.tools.share_image --page path/to/title.png

Run it again if the title page is ever redrawn."""
import argparse
from pathlib import Path

from PIL import Image

from ..app.config import HERE

STATIC = HERE / "app" / "static"
PREVIEW_SIZE = (1200, 630)    # the usual 1.91:1 link-preview shape


def make(page_path):
    page = Image.open(page_path).convert("RGB")
    w, h = page.size
    # The top of the portrait page (title, subtitle and the figure) cropped to the wide preview shape.
    banner = page.crop((0, 0, w, round(w * PREVIEW_SIZE[1] / PREVIEW_SIZE[0]))).resize(PREVIEW_SIZE, Image.LANCZOS)
    banner.save(STATIC / "share-preview.jpg", quality=88, optimize=True)
    # Home-screen icon (iPhone "Add to Home Screen"): the central figure and sky, square.
    side = round(w * 0.72)
    left, top = round((w - side) / 2), round(h * 0.15)
    page.crop((left, top, left + side, top + side)).resize((180, 180), Image.LANCZOS).save(STATIC / "apple-touch-icon.png")
    # The background behind the home, login and sign-up pages: the whole title page, light enough to load quickly.
    page.resize((1000, round(1000 * h / w)), Image.LANCZOS).save(STATIC / "title-bg.jpg", quality=80, optimize=True,
                                                                  progressive=True)
    return STATIC / "share-preview.jpg", STATIC / "apple-touch-icon.png", STATIC / "title-bg.jpg"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--page", default="runs/book/title/page.png")
    args = parser.parse_args(argv)
    for path in make(Path(args.page)):
        print(f"Wrote {path} ({path.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
