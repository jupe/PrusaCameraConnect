import asyncio
import structlog
from PrusaAPI import PrusaConnectAPI, PrusaLinkAPI
from Config import Config, Camera

log = structlog.get_logger()
config = Config()
api = PrusaConnectAPI()


async def camera_loop(camera: Camera):
    while True:
        try:
            if camera.printer_link is not None:
                await log.ainfo(f"Checking printer state for {camera.name}")
                linkapi = PrusaLinkAPI(
                    camera.printer_link.url,
                    camera.printer_link.username,
                    camera.printer_link.password,
                )
                state = await linkapi.get_printer_state()
                if state not in camera.printer_link.snapshot_states:
                    await log.ainfo(
                        f"Skipping snapshot for {camera.name} due to printer state",
                        state=state,
                    )
                    await asyncio.sleep(camera.interval)
                    continue
            await log.ainfo(f"Updating {camera.name}")
            image = await camera.handle.get_snapshot()
            await api.upload_snapshot(camera.token, camera.fingerprint, image)
        except KeyboardInterrupt:
            return
        except Exception as e:
            await log.aexception(f"Job failed for {camera.name}")
        await asyncio.sleep(camera.interval)


async def main():
    await log.ainfo("Starting Prusa Camera Connect")
    tasks = []
    for camera in config.cameras:
        tasks.append(asyncio.create_task(camera_loop(camera)))

    await asyncio.gather(*tasks)
    await log.ainfo("Stopping Prusa Camera Connect")


if __name__ == "__main__":
    asyncio.run(main())
