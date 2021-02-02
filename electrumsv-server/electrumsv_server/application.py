import asyncio
import calendar
import datetime
import sys
import uuid
import logging
import mimetypes
from typing import Optional, Dict

import bitcoinx
import peewee
from aiohttp import web
from aiohttp.web_request import Request
from aiohttp.web_response import Response

from argparse import Namespace
import json
import os

from .database import open_database, PaymentRequest, PaymentRequestOutput
from .exceptions import StartupError
from .constants import DEFAULT_PAGE, RequestState
from .config import parse_args
from .payment_requests import get_next_script


class ApplicationState(object):

    # Todo - return f"<html>Page not found: {filepath}</html>"

    def __init__(self, config: Namespace) -> None:
        self.loop = asyncio.get_event_loop()
        self.config = config
        self.logger = logging.getLogger("application-state")

        wwwroot_path = self._validate_path(config.wwwroot_path)
        if not os.path.exists(os.path.join(wwwroot_path, "index.html")):
            raise StartupError(f"The wwwroot path '{wwwroot_path}' lacks an 'index.html' file.")
        self.wwwroot_path = wwwroot_path

        self.data_path = self._validate_path(config.data_path, create=True)

        self.db = open_database(self)
        self._listeners = []

    def _validate_path(self, path: str, create: bool=False) -> str:
        path = os.path.realpath(path)
        if not os.path.exists(path):
            if not create:
                raise StartupError(f"The path '{path}' does not exist.")
            os.makedirs(path)
        return path

    def register_listener(self, ws) -> None:
        self._listeners.append(ws)

    def unregister_listener(self, ws) -> None:
        self._listeners.remove(ws)

    async def notify_listeners(self, value) -> None:
        text = json.dumps(value)
        for ws in self._listeners:
            await ws.send(text)

    # ----- WEBSITE ----- #

    async def serve_file(self, request: web.Request, filename: Optional[str] = None) -> Response:
        filepath = request.path[1:].split("/")
        try:
            if filepath == [""]:
                filepath = [DEFAULT_PAGE]
            page_path = os.path.realpath(os.path.join(self.wwwroot_path, *filepath))
            if not page_path.startswith(self.wwwroot_path) or not os.path.exists(page_path):
                print("..... filename %r", page_path)
                raise FileNotFoundError

            content_type, encoding_name = mimetypes.guess_type(filepath[-1])
            with open(page_path, 'rb') as f:
                content = f.read()
                return web.Response(body=content, content_type=content_type)

        except FileNotFoundError:
            return web.Response(body=f"<html>Page not found: {filepath}</html>", status=404)
        except Exception:
            self.logger.exception("Rendering page failed unexpectedly")
            return web.Response(status=500)

    # ----- API -----#

    async def create_invoice(self, request: web.Request) -> web.Response:
        data = await request.json()

        if type(data) is not dict:
            return web.Response(body="invalid payment data type", status=400)

        description = data.get("description")
        if description is not None:
            if type(description) is not str:
                return web.Response(body="invalid payment description type", status=400)
            if not description.strip():
                description = None

        output_list = data.get("outputs")
        if type(output_list) is not list:
            return web.Response(body="invalid payment outputs type", status=400)

        expiration_minutes = data.get("expiration")
        if type(expiration_minutes) is not int:
            return web.Response(body="invalid payment expiration value", status=400)

        request_uid = uuid.uuid4()
        date_created = datetime.datetime.utcnow()
        if expiration_minutes == 0:
            date_expires = None
        else:
            date_expires = date_created + datetime.timedelta(minutes=expiration_minutes)
        request = PaymentRequest(uid=request_uid, description=description,
            date_created=date_created, date_expires=date_expires, state=RequestState.UNPAID)

        database_outputs = []
        response_outputs = []
        for amount_entry in output_list:
            description, amount = amount_entry
            assert description is None or type(description) is str and len(description) < 100
            assert type(amount) is int
            script = get_next_script()
            database_outputs.append(
                PaymentRequestOutput(description=description, amount=amount, script=script,
                    request=request_uid))
            response_outputs.append({"description": description, "amount": amount})

        with self.db.atomic():
            PaymentRequest.bulk_create([request])
            PaymentRequestOutput.bulk_create(database_outputs, batch_size=100)

        return web.Response(body=json.dumps(request_uid.hex), status=200)

    async def _get_invoice(self, invoice_id: uuid.UUID,
            for_display: bool = False) -> Dict:
        pr = (PaymentRequest.select(PaymentRequest, PaymentRequestOutput).join(
            PaymentRequestOutput).where(PaymentRequest.uid == invoice_id)).get()

        outputs_object = []
        for output in pr.outputs:
            outputs_object.append({"description": output.description, "amount": output.amount,
                "script": output.script.hex()})

        id_text = str(invoice_id)
        paymentRequestData = {"network": "bitcoin-sv", "memo": pr.description,
            "paymentUrl": f"http://127.0.0.1:{self.config.http_server_port}/api/bip270/{id_text}",
            "outputs": outputs_object,
            "creationTimestamp": calendar.timegm(pr.date_created.utctimetuple()),
            "expirationTimestamp": calendar.timegm(
                pr.date_expires.utctimetuple()) if pr.date_expires else None, }
        if for_display:
            paymentRequestData["id"] = id_text
            paymentRequestData["state"] = pr.state
        return paymentRequestData

    async def get_invoice(self, request: Request) -> Response:
        id_text = request.match_info['id_text']
        request_id = uuid.UUID(hex=id_text)
        result = await self._get_invoice(request_id)
        return web.Response(body=json.dumps(result), status=200)

    async def get_invoice_display_state(self, request: Request) -> Response:
        id_text = request.match_info['id_text']
        request_id = uuid.UUID(hex=id_text)
        result = {"paymentRequest": await self._get_invoice(request_id, for_display=True),
            "paymentUri": f"bitcoin:?r=http://127.0.0.1:"
                          f"{self.config.http_server_port}/api/bip270/{id_text}&sv", }
        return web.Response(body=json.dumps(result), status=200)

    async def cancel_invoice(self, request: Request) -> Response:
        id_text = request.match_info['id_text']
        request_id = uuid.UUID(hex=id_text)

        # Mark the invoice as paid by the given transaction.
        query = (PaymentRequest.update({PaymentRequest.state: RequestState.CLOSED, }).where(
            PaymentRequest.uid == request_id.bytes))
        query.execute()
        return web.Response(body=json.dumps(True), status=200)

    async def submit_invoice_payment(self, request: Request) -> Response:
        id_text = request.match_info['id_text']
        payment_object = await request.json()

        content_type = request.headers.get('Content-Type')
        if content_type != "application/bitcoinsv-payment":
            return web.Response(body=content_type, status=web.HTTPUnsupportedMediaType.status_code)

        accept_content_type = request.headers.get('Accept')
        if accept_content_type != "application/bitcoinsv-paymentack":
            return web.Response(body=accept_content_type, status=web.HTTPNotAcceptable.status_code)

        request_id = uuid.UUID(hex=id_text)
        pr = (PaymentRequest.select(PaymentRequest, PaymentRequestOutput).join(
            PaymentRequestOutput).where(PaymentRequest.uid == request_id.bytes)).get()

        # Verify that the transaction is complete.
        if type(payment_object) is not dict:
            return web.Response(body="invalid payment object", status=400)
        if "transaction" not in payment_object:
            return web.Response(body="payment object lacks transaction", status=400)

        try:
            tx = bitcoinx.Tx.from_hex(payment_object["transaction"])
        except (TypeError, ValueError):
            # TypeError: from_hex gets non string.
            # ValueError: from_hex gets invalid hex encoded data.
            return web.Response(body="Invoice has an invalid payment transaction", status=400)

        if pr.tx_hash is None:
            self.logger.debug("Attempting to settle payment request with tx '%s'", tx.hex_hash())

            # Verify that the outputs are present.
            tx_outputs = {bytes(out.script_pubkey): out.value for out in tx.outputs}
            try:
                for output in pr.outputs:
                    if output.amount != tx_outputs[output.script]:
                        return web.Response(body="Invoice has an invalid output amount",
                            status=400)
            except KeyError:
                return web.Response(body="Invoice has a missing output", status=400)

            # TODO: Broadcast it.
            # Broadcasting the transaction verifies that the transaction is valid.

            # TODO: If it fails to broadcast handle it.

            # Mark the invoice as paid by the given transaction.
            query = (PaymentRequest.update({PaymentRequest.tx_hash: tx.hash(),
                PaymentRequest.state: RequestState.PAID, }).where(
                PaymentRequest.uid == request_id.bytes))
            query.execute()

            self.logger.debug("Payment request '%s' paid with tx '%s'", request_id, tx.hex_hash())

            await self.notify_listeners(
                ["InvoicePaid", id_text])  # TODO: Notify any connected listener.
        elif pr.tx_hash != tx.hash():
            return web.Response(body="Invoice already paid with different payment", status=400)

        ack_object = {"payment": payment_object, }
        return web.Response(body=json.dumps(ack_object), headers={'Content-Type':
            'application/bitcoinsv-paymentack', }, status=200)

    async def get_invoices(self, request: Request) -> Response:
        sort_order = request.query.get('order', "desc")
        offset = int(request.query.get('offset'))
        page_size = int(request.query.get('limit'))
        sort_column = request.query.get('sort', "creationTimestamp")
        filter_text = request.query.get('filter', None)

        current_page = (offset / page_size) + 1

        query = (PaymentRequest.select(PaymentRequest,
            peewee.fn.SUM(PaymentRequestOutput.amount).alias("amount")).join(
            PaymentRequestOutput).group_by(PaymentRequest.uid))

        if filter_text is not None:
            filter_data = json.loads(filter_text)
            for filter_key, filter_values in filter_data.items():
                if len(filter_values):
                    if filter_key == "state":
                        query = query.orwhere(PaymentRequest.state == filter_values)
                    else:
                        self.logger.error("get_invoices with unknown filter key: %s", filter_key)

        sort_key = PaymentRequest.date_created
        if sort_column == "creationTimestamp":
            sort_key = PaymentRequest.date_created
        elif sort_column == "expirationTimestamp":
            sort_key = PaymentRequest.date_expires
        elif sort_column == "description":
            sort_key = PaymentRequest.description
        elif sort_column == "state":
            sort_key = PaymentRequest.state
        elif sort_column == "amount":
            sort_key = PaymentRequestOutput.amount

        if sort_order == "desc":
            sort_key = -sort_key

        query = query.order_by(sort_key)

        results = query.paginate(current_page, page_size).objects()
        result_count = query.count()  # pylint: disable=no-value-for-parameter

        data = {"total": result_count, "totalNotFiltered": result_count, "rows": [
            {"id": r.uid.hex, "state": r.state,
                "creationTimestamp": calendar.timegm(r.date_created.utctimetuple()),
                "expirationTimestamp": calendar.timegm(
                    r.date_expires.utctimetuple()) if r.date_expires else None,
                "description": r.description, "amount": r.amount,
                "tx_hash": r.tx_hash.hex() if r.tx_hash else None, } for r in results], }

        return web.Response(body=json.dumps(data), status=200)

    # Todo - Need a websocket that will be used to notify the client (browser) of paid invoices
    #  this will likely be via relaying tx state changes on from ElectrumSV
    # async def websocket_events(app: Application, request: Request,
    #         websocket: websockets.Websocket) -> None:
    #     app.register_listener(websocket)
    #     while not websocket.closed:
    #         # Discard any incoming messages.
    #         msg = await websocket.recv()
    #     app.unregister_listener(websocket)


def add_base_routes(web_app: web.Application, app_state: ApplicationState):
    web_app.add_routes([
        web.get("/", app_state.serve_file),
    ])
    return web_app


def add_website_routes(web_app: web.Application, app_state: ApplicationState):
    web_paths = []
    for root_path, dirnames, filenames in os.walk(app_state.wwwroot_path):
        if len(filenames):
            web_path = os.path.relpath(root_path, app_state.wwwroot_path).replace(
                os.path.sep, "/")
            web_paths.append(web_path)

    # Deeper paths need to be routed first so as to not override shallower paths.
    for web_path in sorted(web_paths, key=len, reverse=True):
        if web_path == ".":
            web_app.add_routes([web.get("/{filename}", app_state.serve_file), ])
        else:
            web_app.add_routes(
                [web.get("/" + web_path + "/{filename}", app_state.serve_file), ])
    return web_app


def add_api_routes(web_app: web.Application, app_state: ApplicationState):
    web_app.add_routes([
        web.get("/api/bip270", app_state.get_invoices),
        web.post("/api/bip270", app_state.create_invoice),
        web.get("/api/bip270/{id_text}/display", app_state.get_invoice_display_state),
        web.post("/api/bip270/{id_text}/cancel", app_state.cancel_invoice),
        web.get("/api/bip270/{id_text}", app_state.get_invoice),
        web.post("/api/bip270/{id_text}", app_state.submit_invoice_payment),
    ])

    # server.websocket("/events")(partial(bip270.websocket_events, app))
    return web_app


def run() -> None:
    try:
        logging.basicConfig(format='%(asctime)s %(levelname)-8s %(name)-24s %(message)s',
            level=logging.DEBUG,
            datefmt='%Y-%m-%d %H:%M:%S')

        config = parse_args()
        app_state = ApplicationState(config)
        web_app = web.Application()
        web_app.app_state = app_state

        # routes
        web_app = add_base_routes(web_app, app_state)
        web_app = add_website_routes(web_app, app_state)
        web_app = add_api_routes(web_app, app_state)

        web.run_app(web_app, host="127.0.0.1", port=24242)  # type: ignore
    except StartupError as e:
        sys.exit(e)
