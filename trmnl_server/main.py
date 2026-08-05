"""FastAPI application for TRMNL BYOS server."""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from trmnl_server.config import get_config
from trmnl_server.database import close_db, init_db
from trmnl_server.routes import api, settings
from trmnl_server.services.plugins import register_plugins, schedule_plugins
from trmnl_server.services.scheduler import start_scheduler, stop_scheduler


def _configure_logging() -> None:
    """Set the root log level from LOG_LEVEL or server.log_level (default INFO).

    The device handlers log request detail at DEBUG. With the level hardcoded, the
    only way to see any of it in a container was to rebuild the image.
    """
    level_name = os.getenv("LOG_LEVEL")
    if not level_name:
        try:
            level_name = get_config().log_level
        except Exception:  # pragma: no cover - config problems surface later
            level_name = "INFO"

    level = getattr(logging, level_name.strip().upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    logging.getLogger().setLevel(level)


_configure_logging()
logger = logging.getLogger(__name__)


class NoCacheStaticFiles(StaticFiles):
    """StaticFiles that disables caching.

    The generated e-ink screen is rewritten in place (same filename), so any
    caching by the device, browser, or Cloudflare serves a stale image. Force a
    revalidation on every request.
    """

    def is_not_modified(self, response_headers, request_headers) -> bool:  # noqa: ANN001
        return False

    async def get_response(self, path: str, scope) -> Response:  # noqa: ANN001
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        for stale in ("etag", "last-modified"):
            if stale in response.headers:
                del response.headers[stale]
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    config = get_config()

    logger.info("Starting TRMNL BYOS Server...")
    logger.info(f"Server: {config.host}:{config.port}")
    logger.info(f"Generated dir: {config.generated_dir}")

    # Initialize database
    await init_db()
    logger.info("Database initialized")

    # Discover and register plugins
    plugins = register_plugins()
    logger.info(f"Registered {len(plugins)} plugins")

    # Start scheduler
    await start_scheduler()

    # Schedule plugin refresh jobs
    schedule_plugins()
    logger.info("Plugin refresh jobs scheduled")

    yield

    # Shutdown
    logger.info("Shutting down...")
    await stop_scheduler()
    await close_db()
    logger.info("Shutdown complete")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    config = get_config()

    app = FastAPI(
        title="MunichGlance TRMNL Server",
        description="TRMNL BYOS server with MunichGlance plugin for Munich transit and weather",
        version="0.1.0",
        lifespan=lifespan,
    )

    # CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Request logging middleware
    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        logger.debug(f"{request.method} {request.url.path}")
        response = await call_next(request)
        return response

    # Include routers
    app.include_router(api.router)
    app.include_router(settings.router)

    # Mount static directories
    if config.generated_dir.exists():
        app.mount(
            "/generated",
            NoCacheStaticFiles(directory=str(config.generated_dir)),
            name="generated",
        )

    if config.web_dir.exists():
        app.mount(
            "/static",
            StaticFiles(directory=str(config.web_dir)),
            name="static",
        )

    # Root endpoint
    @app.get("/", response_class=HTMLResponse)
    async def root():
        """Dashboard landing page."""
        config = get_config()
        web_index = config.web_dir / "index.html"

        if web_index.exists():
            return FileResponse(web_index)

        # Simple fallback page
        return """
        <!DOCTYPE html>
        <html>
        <head>
            <title>MunichGlance</title>
            <style>
                body {
                    font-family: system-ui, -apple-system, sans-serif;
                    max-width: 800px;
                    margin: 0 auto;
                    padding: 2rem;
                    background: #f5f5f5;
                }
                h1 { color: #333; }
                .card {
                    background: white;
                    padding: 1.5rem;
                    border-radius: 8px;
                    box-shadow: 0 2px 4px rgba(0,0,0,0.1);
                    margin: 1rem 0;
                }
                a { color: #0066cc; }
                code {
                    background: #eee;
                    padding: 0.2rem 0.4rem;
                    border-radius: 4px;
                    font-size: 0.9em;
                }
            </style>
        </head>
        <body>
            <h1>MunichGlance TRMNL Server</h1>

            <div class="card">
                <h2>Status</h2>
                <p>Server is running. Check <a href="/api/health">/api/health</a> for status.</p>
            </div>

            <div class="card">
                <h2>API Endpoints</h2>
                <ul>
                    <li><code>GET /api/display</code> - Get display image</li>
                    <li><code>POST /api/log</code> - Device logging</li>
                    <li><code>POST /api/battery</code> - Battery telemetry</li>
                    <li><code>GET /api/setup</code> - Device setup</li>
                    <li><code>GET /settings</code> - Server settings</li>
                </ul>
                <p>See <a href="/docs">/docs</a> for full API documentation.</p>
            </div>

            <div class="card">
                <h2>Plugins</h2>
                <p>View registered plugins at <a href="/settings/plugins">/settings/plugins</a></p>
            </div>
        </body>
        </html>
        """

    # BMP endpoints for TRMNL (alternating for cache busting)
    @app.get("/image/screen.bmp")
    @app.get("/image/screen1.bmp")
    async def get_screen_bmp():
        """Serve current screen BMP image."""
        config = get_config()

        # Find the most recent BMP in generated dir
        from trmnl_server.services.plugins import get_cached_output, get_primary_plugin_name

        plugin_name = get_primary_plugin_name()
        if plugin_name:
            output = get_cached_output(plugin_name)
            if output and output.bmp_path and output.bmp_path.exists():
                return FileResponse(
                    output.bmp_path,
                    media_type="image/bmp",
                    filename="screen.bmp",
                )

        # Fallback to any BMP in generated dir
        for bmp_file in config.generated_dir.rglob("*.bmp"):
            return FileResponse(
                bmp_file,
                media_type="image/bmp",
                filename="screen.bmp",
            )

        return {"error": "No BMP image available"}

    # PNG endpoints for preview
    @app.get("/image/grayscale.png")
    @app.get("/image/grayscale1.png")
    async def get_screen_png():
        """Serve current screen PNG image."""
        config = get_config()

        from trmnl_server.services.plugins import get_cached_output, get_primary_plugin_name

        plugin_name = get_primary_plugin_name()
        if plugin_name:
            output = get_cached_output(plugin_name)
            if output and output.png_path and output.png_path.exists():
                return FileResponse(
                    output.png_path,
                    media_type="image/png",
                    filename="screen.png",
                )

        # Fallback to any PNG in generated dir
        for png_file in config.generated_dir.rglob("*.png"):
            return FileResponse(
                png_file,
                media_type="image/png",
                filename="screen.png",
            )

        return {"error": "No PNG image available"}

    return app


# Create app instance for uvicorn
app = create_app()
