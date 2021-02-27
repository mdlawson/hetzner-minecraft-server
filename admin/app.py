import os
import time

from mcstatus import MinecraftServer

from hcloud import Client as HetznerClient
from hcloud.images.domain import Image
from hcloud.server_types.domain import ServerType
from hcloud.datacenters.domain import Datacenter

from tornado.web import RequestHandler, Application, url
from tornado.ioloop import IOLoop, PeriodicCallback

MINECRAFT_HOST = os.environ.get("MINECRAFT_HOST")
MINECRAFT_PORT = 25565
MINECRAFT_RCON_PORT = 25575
RCON_PASSWORD = os.environ.get("RCON_PASSWORD")

HETZNER_API_TOKEN = os.environ.get("HETZNER_API_TOKEN")


DEBUG = os.environ.get("DEBUG") == "true"
IDLE_CHECK_INTERVAL = (10 if not DEBUG else 0.2) * 60 * 1000

server = MinecraftServer(MINECRAFT_HOST, MINECRAFT_PORT)
hetzner = HetznerClient(token=HETZNER_API_TOKEN, poll_interval=5)


def create():
    image, *rest = hetzner.images.get_all(
        label_selector="active", sort=["created:desc"]
    )
    key = hetzner.ssh_keys.get_by_name(MINECRAFT_HOST)
    print("Creating server...")
    result = hetzner.servers.create(
        name=MINECRAFT_HOST,
        server_type=ServerType(name=image.labels["server_type"]),
        image=image,
        ssh_keys=[key],
        datacenter=Datacenter(name=image.labels["datacenter"]),
    )
    print("Creation requested, waiting for create...")
    result.action.wait_until_finished()
    print("Server created, waiting for start...")
    for action in result.next_actions:
        action.wait_until_finished()
    print("Done! Cleaning up...")
    image.delete()
    for old in rest:
        old.delete()

    print("Created!")


def destroy():
    server = hetzner.servers.get_by_name(MINECRAFT_HOST)

    print("Telling server to stop...")
    server.shutdown().wait_until_finished()

    print("Creating image...")
    result = server.create_image(
        labels={
            "server_type": server.server_type.name,
            "datacenter": server.datacenter.name,
            "active": "",
        },
    )
    result.action.wait_until_finished()

    print("Deleting server...")
    server.delete().wait_until_finished()

    print("Destroyed!")
    destroy_callback.stop()


def get_host_status():
    server = hetzner.servers.get_by_name(MINECRAFT_HOST)
    if server is None:
        return "destroyed", None
    return server.status, server.public_net.ipv4.ip


def idle():
    try:
        status = server.status()
        print(f"The server has {status.players.online} players")
        if status.players.online > 0:
            destroy_callback.stop()
        elif not destroy_callback.is_running():
            print(
                f"Server is idle, will stop in {IDLE_CHECK_INTERVAL * 2} mins",
            )
            destroy_callback.start()
    except Exception:
        print("The server is not running")
        status = get_host_status()
        if status != "destroyed" and not destroy_callback.is_running():
            destroy_callback.start()


idle_callback = PeriodicCallback(idle, callback_time=IDLE_CHECK_INTERVAL)
destroy_callback = PeriodicCallback(
    lambda: IOLoop.current().run_in_executor(func=destroy, executor=None),
    callback_time=IDLE_CHECK_INTERVAL * 2,
)


class MainHandler(RequestHandler):
    def get(self):
        host_status, ip = get_host_status()

        if host_status != "running":
            self.render(
                "admin.html",
                online=False,
                players=[],
                host_status=host_status,
                is_idle=False,
                auto_destroy=idle_callback.is_running(),
            )
            return

        try:
            minecraft_status = server.status()
        except OSError:
            self.render(
                "admin.html",
                online=False,
                players=[],
                host_status=host_status,
                is_idle=False,
                auto_destroy=idle_callback.is_running(),
            )
            return

        self.render(
            "admin.html",
            online=True,
            minecraft_status=minecraft_status,
            host_status=host_status,
            ip=ip,
            is_idle=destroy_callback.is_running(),
            auto_destroy=idle_callback.is_running(),
        )


class CreateHandler(RequestHandler):
    def post(self):
        IOLoop.current().run_in_executor(func=create, executor=None)
        self.redirect("/")


class DestroyHandler(RequestHandler):
    def post(self):
        IOLoop.current().run_in_executor(func=destroy, executor=None)
        self.redirect("/")


class AutoDestroyHandler(RequestHandler):
    def post(self):
        if idle_callback.is_running():
            idle_callback.stop()
            destroy_callback.stop()
        else:
            idle_callback.start()
        self.redirect("/")


def make_app():
    return Application(
        [
            url(r"/", MainHandler),
            url(r"/auto_destroy", AutoDestroyHandler, name="auto_destroy"),
            url(r"/create", CreateHandler, name="create"),
            url(r"/destroy", DestroyHandler, name="destroy"),
        ],
        debug=DEBUG,
    )


if __name__ == "__main__":
    app = make_app()
    app.listen(8080)
    print("Server is listening")
    idle_callback.start()
    IOLoop.current().start()