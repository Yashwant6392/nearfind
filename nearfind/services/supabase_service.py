import errno
import socket
import ssl
from types import SimpleNamespace
from urllib.parse import quote, urlsplit

import httpx
from supabase import create_client
from config import Config


def _headers(key, content_type="application/json"):
    headers = {
        "apikey": key,
    }
    if not key.startswith(("sb_publishable_", "sb_secret_")):
        headers["Authorization"] = f"Bearer {key}"
    if content_type:
        headers["Content-Type"] = content_type
    return headers


class SupabaseRequestError(RuntimeError):
    def __init__(self, status_code, message, code=None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.code = code


class SupabaseConfigurationError(RuntimeError):
    def __init__(self, message):
        super().__init__(message)
        self.public_message = message


class SupabaseConnectionError(RuntimeError):
    def __init__(self, diagnostic, exception_types):
        super().__init__("Supabase Auth is unavailable")
        self.diagnostic = diagnostic
        self.exception_types = exception_types


def _exception_chain(error):
    chain = []
    seen = set()
    current = error
    while current is not None and id(current) not in seen:
        chain.append(current)
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return chain


def _transport_diagnostic(error):
    chain = _exception_chain(error)
    if any(isinstance(item, httpx.TimeoutException) for item in chain):
        return "timeout"
    if any(
        isinstance(item, httpx.ProxyError)
        or (type(item).__name__ == "ProxyError" and type(item).__module__.startswith("httpcore"))
        for item in chain
    ):
        return "proxy"
    if any(isinstance(item, ssl.SSLError) for item in chain):
        return "tls"
    if any(isinstance(item, socket.gaierror) for item in chain):
        return "dns"
    if any(
        isinstance(item, ConnectionRefusedError)
        or (isinstance(item, OSError) and item.errno == errno.ECONNREFUSED)
        for item in chain
    ):
        return "connection_refused"
    if isinstance(error, httpx.ConnectError):
        return "connect"
    return "network"


def supabase_auth_request(auth, method, credentials):
    try:
        return getattr(auth, method)(credentials)
    except httpx.RequestError as error:
        raise SupabaseConnectionError(
            _transport_diagnostic(error),
            tuple(type(item).__name__ for item in _exception_chain(error)),
        ) from None
    except (SupabaseConnectionError, SupabaseRequestError):
        raise
    except Exception as error:
        status = getattr(error, "status", None) or getattr(error, "status_code", None)
        if status is not None:
            code = getattr(error, "code", None)
            message = getattr(error, "message", None) or str(error)
            raise SupabaseRequestError(status, message, code=code) from error
        raise


def _raise_for_supabase(response):
    if response.status_code >= 400:
        code = None
        try:
            body = response.json()
            if isinstance(body, dict):
                detail = body.get("message") or body.get("msg") or body.get("error_description") or body.get("error") or response.text
                code = body.get("code") or body.get("error_code")
            else:
                detail = response.text
        except ValueError:
            detail = response.text
        raise SupabaseRequestError(response.status_code, detail, code)


class RestResult:
    def __init__(self, data=None):
        self.data = data


class RestQuery:
    def __init__(self, client, table_name):
        self.client = client
        self.table_name = table_name
        self.method = "GET"
        self.payload = None
        self.params = {}
        self.filters = []
        self.single_row = False

    def select(self, columns):
        self.method = "GET"
        self.params["select"] = columns
        return self

    def insert(self, payload):
        self.method = "POST"
        self.payload = payload
        self.params["select"] = "*"
        return self

    def update(self, payload):
        self.method = "PATCH"
        self.payload = payload
        self.params["select"] = "*"
        return self

    def eq(self, column, value):
        self.filters.append((column, f"eq.{value}"))
        return self

    def lt(self, column, value):
        self.filters.append((column, f"lt.{value}"))
        return self

    def gte(self, column, value):
        self.filters.append((column, f"gte.{value}"))
        return self

    def lte(self, column, value):
        self.filters.append((column, f"lte.{value}"))
        return self

    def in_(self, column, values):
        clean_values = ",".join(str(value) for value in values)
        self.filters.append((column, f"in.({clean_values})"))
        return self

    def order(self, column, desc=False):
        self.params["order"] = f"{column}.{'desc' if desc else 'asc'}"
        return self

    def limit(self, count):
        self.params["limit"] = str(count)
        return self

    def single(self):
        self.single_row = True
        self.params.setdefault("limit", "1")
        return self

    def maybe_single(self):
        self.single_row = True
        self.params.setdefault("limit", "1")
        return self

    def execute(self):
        url = f"{self.client.rest_url}/{self.table_name}"
        params = list(self.params.items()) + self.filters
        headers = _headers(self.client.key)
        if self.method in {"POST", "PATCH"}:
            headers["Prefer"] = "return=representation"
        response = self.client.http.request(self.method, url, params=params, json=self.payload, headers=headers)
        _raise_for_supabase(response)
        if not response.content:
            return RestResult(None)
        data = response.json()
        if self.single_row and isinstance(data, list):
            data = data[0] if data else None
        return RestResult(data)


class RestRpcQuery:
    def __init__(self, client, function_name, payload):
        self.client = client
        self.function_name = function_name
        self.payload = payload

    def execute(self):
        response = self.client.http.post(
            f"{self.client.rest_url}/rpc/{self.function_name}",
            headers=_headers(self.client.key),
            json=self.payload,
        )
        _raise_for_supabase(response)
        if not response.content:
            return RestResult(None)
        return RestResult(response.json())


class RestTableClient:
    def __init__(self, client, table_name):
        self.client = client
        self.table_name = table_name

    def select(self, columns):
        return RestQuery(self.client, self.table_name).select(columns)

    def insert(self, payload):
        return RestQuery(self.client, self.table_name).insert(payload)

    def update(self, payload):
        return RestQuery(self.client, self.table_name).update(payload)


class RestAuthClient:
    def __init__(self, client):
        self.client = client

    def sign_up(self, credentials):
        response = self.client.http.post(
            f"{self.client.auth_url}/signup",
            headers=_headers(self.client.publishable_key),
            json=credentials,
        )
        _raise_for_supabase(response)
        data = response.json()
        user = data.get("user") or data
        return SimpleNamespace(user=SimpleNamespace(id=user.get("id"), email=user.get("email")) if user else None)

    def sign_in_with_password(self, credentials):
        response = self.client.http.post(
            f"{self.client.auth_url}/token",
            params={"grant_type": "password"},
            headers=_headers(self.client.publishable_key),
            json=credentials,
        )
        _raise_for_supabase(response)
        data = response.json()
        user = data.get("user")
        return SimpleNamespace(user=SimpleNamespace(id=user.get("id"), email=user.get("email")) if user else None)


class RestBucketClient:
    def __init__(self, client, bucket):
        self.client = client
        self.bucket = bucket

    def upload(self, path, data, options=None):
        options = options or {}
        content_type = options.get("content-type") or options.get("Content-Type") or "application/octet-stream"
        headers = _headers(self.client.key, content_type=content_type)
        if options.get("x-upsert"):
            headers["x-upsert"] = options["x-upsert"]
        url = f"{self.client.storage_url}/object/{self.bucket}/{quote(path)}"
        response = self.client.http.post(url, headers=headers, content=data)
        _raise_for_supabase(response)
        return response.json() if response.content else {}

    def get_public_url(self, path):
        return f"{self.client.storage_url}/object/public/{self.bucket}/{quote(path)}"


class RestStorageClient:
    def __init__(self, client):
        self.client = client

    def from_(self, bucket):
        return RestBucketClient(self.client, bucket)


DEFAULT_TIMEOUT_SECONDS = 20.0


class RestSupabaseClient:
    def __init__(self, url, key, publishable_key):
        self.url = url.rstrip("/")
        self.key = key
        self.publishable_key = publishable_key
        self.rest_url = f"{self.url}/rest/v1"
        self.auth_url = f"{self.url}/auth/v1"
        self.storage_url = f"{self.url}/storage/v1"
        self.http = httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS, trust_env=True)
        self.auth = RestAuthClient(self)
        self.storage = RestStorageClient(self)

    def table(self, name):
        return RestTableClient(self, name)

    def rpc(self, function_name, payload):
        return RestRpcQuery(self, function_name, payload)


def get_supabase(admin=False):
    if not Config.SUPABASE_URL:
        raise SupabaseConfigurationError("SUPABASE_URL is not configured.")
    try:
        parsed_url = urlsplit(Config.SUPABASE_URL)
        valid_url = parsed_url.scheme in {"http", "https"} and bool(parsed_url.hostname)
    except ValueError:
        valid_url = False
    if not valid_url:
        raise SupabaseConfigurationError("SUPABASE_URL must be a valid HTTP(S) URL.")
    key = Config.SUPABASE_SERVICE_ROLE_KEY if admin else Config.SUPABASE_ANON_KEY
    if not key:
        missing = "SUPABASE_SERVICE_ROLE_KEY" if admin else "SUPABASE_ANON_KEY"
        raise SupabaseConfigurationError(f"{missing} is not configured.")
    if key.startswith(("sb_publishable_", "sb_secret_")):
        return RestSupabaseClient(Config.SUPABASE_URL, key, Config.SUPABASE_ANON_KEY)
    return create_client(Config.SUPABASE_URL, key)


def table(name):
    return get_supabase(admin=True).table(name)


def rpc(function_name, payload):
    return get_supabase(admin=True).rpc(function_name, payload).execute().data
