"""Public business information. Only explicitly registered static files are served."""
from pathlib import Path

from fastapi.responses import FileResponse

ASSETS = Path(__file__).with_name('public')


def install_public_pages(app):
    def page(filename, media_type):
        async def serve():
            return FileResponse(ASSETS / filename, media_type=media_type, headers={
                'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
                'X-Content-Type-Options': 'nosniff',
                'Referrer-Policy': 'no-referrer',
                'Cache-Control': 'public, max-age=300',
            })
        return serve

    for path, filename, media in (
        ('/about', 'about.html', 'text/html'),
        ('/privacy', 'privacy.html', 'text/html'),
        ('/data-deletion', 'data-deletion.html', 'text/html'),
        ('/app-icon.png', 'app-icon.png', 'image/png'),
    ):
        app.add_api_route(path, page(filename, media), methods=['GET'], include_in_schema=False)
