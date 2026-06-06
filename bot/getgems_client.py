"""Getgems marketplace client — reads prices via REST API, auto-lists via Playwright.

Price reading: Getgems public REST API (api.getgems.io/public-api).
  No browser needed — works on Railway / headless environments.

Auto-listing: Playwright CDP + TON Connect wallet auth (ton_proof + signData).
  Requires browser — only for environments with Chrome/CDP.
"""

import asyncio
import hashlib
import logging
import struct
import time
from collections.abc import Callable, Coroutine
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

NANO = 1_000_000_000
PUBLIC_API_BASE = "https://api.getgems.io/public-api"


class GetgemsClient:
    """Client for Getgems marketplace: price reading + auto-listing."""

    def __init__(
        self,
        api_key: str = "",
        cdp_url: str = "http://localhost:29229",
        graphql_auth_token: str = "",
    ) -> None:
        self._api_key = api_key
        self._cdp_url = cdp_url
        self._session: aiohttp.ClientSession | None = None
        self._chain_session: aiohttp.ClientSession | None = None  # for toncenter/tonapi
        self._auth_token: str | None = None
        self._user_token: str | None = None  # ton-proof user auth token
        self._graphql_token: str = graphql_auth_token  # website AUTH_TOKEN for GraphQL
        self._wallet_address: str | None = None
        self._keypair: Any = None  # nacl signing key
        self._secret_key: bytes = b""
        self._public_key: bytes = b""

        # Playwright
        self._pw: Any = None
        self._browser: Any = None

        # Auth tracking
        self._auth_alert_sent = False
        self._on_auth_expired: Callable[[], Coroutine[Any, Any, None]] | None = None

        # Cache: collection_name -> collection_address
        self._collection_addresses: dict[str, str] = {}
        self._collections_failed: bool = False
        self._collections_fail_time: float = 0

    def set_auth_expired_callback(
        self, cb: Callable[[], Coroutine[Any, Any, None]]
    ) -> None:
        self._on_auth_expired = cb

    def update_api_key(self, api_key: str) -> None:
        """Update Getgems API key at runtime."""
        self._api_key = api_key
        self._auth_alert_sent = False
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._reset_session())
        except RuntimeError:
            # No running event loop — session will be recreated on next use
            self._session = None
        logger.info("Getgems API key updated")

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        if self._chain_session and not self._chain_session.closed:
            await self._chain_session.close()
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass

    # ── HTTP session ─────────────────────────────────────────────────────

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            auth = self._user_token or self._api_key or ""
            self._session = aiohttp.ClientSession(
                headers={
                    "Authorization": f"Bearer {auth}" if auth else "",
                    "Accept": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    async def _reset_session(self) -> None:
        """Force session recreation (e.g. after auth token update)."""
        if self._session and not self._session.closed:
            try:
                await self._session.close()
            except Exception:
                pass
        self._session = None

    async def _api_get(
        self, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """GET request to Getgems public API (uses API key, not user token)."""

        session = await self._get_session()
        url = f"{PUBLIC_API_BASE}{path}"
        # Override auth header with API key for public endpoints
        headers = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        for attempt in range(3):
            try:
                async with session.get(url, params=params, headers=headers) as resp:
                    if resp.status == 429:
                        wait = min(5.0 * (attempt + 1), 30.0)
                        logger.warning("Getgems rate limited, waiting %.0fs", wait)
                        await asyncio.sleep(wait)
                        continue
                    if resp.status == 401:
                        logger.error("Getgems API key invalid or expired")
                        if not self._auth_alert_sent:
                            self._auth_alert_sent = True
                            if self._on_auth_expired:
                                try:
                                    await self._on_auth_expired()
                                except Exception:
                                    pass
                        return None
                    if resp.status != 200:
                        text = await resp.text()
                        logger.error("Getgems API %d on %s: %s", resp.status, path, text[:200])
                        return None

                    data = await resp.json()
                    if not data.get("success"):
                        logger.warning("Getgems API returned success=false for %s", path)
                        return None
                    return data.get("response")

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.error("Getgems request error on %s: %s", path, e)
                await asyncio.sleep(2.0)

        return None

    # ── Collection mapping ───────────────────────────────────────────────

    async def load_gift_collections(self) -> dict[str, str]:
        """Load gift collection name -> address mapping. Returns cached if available."""
        if self._collection_addresses:
            return self._collection_addresses
        if self._collections_failed:
            # Retry after 5 minutes
            import time as _time

            if _time.monotonic() - self._collections_fail_time < 300:
                return self._collection_addresses
            self._collections_failed = False

        resp = await self._api_get("/v1/gifts/collections", {"limit": 100})
        if not resp:
            self._collections_failed = True
            import time as _time

            self._collections_fail_time = _time.monotonic()
            logger.warning("Getgems: /v1/gifts/collections unavailable, retrying in 5m")
            return self._collection_addresses

        for item in resp.get("items", []):
            name = item.get("name", "")
            addr = item.get("address", "")
            if name and addr:
                self._collection_addresses[name] = addr

        logger.info("Getgems: loaded %d gift collection addresses", len(self._collection_addresses))
        return self._collection_addresses

    def _find_collection_address(self, name: str) -> str:
        """Fuzzy lookup: find collection address by name (plural/singular)."""
        addr = self._collection_addresses.get(name, "")
        if addr:
            return addr
        for suffix in ("s", "es"):
            addr = self._collection_addresses.get(name + suffix, "")
            if addr:
                return addr
        if name.endswith("s"):
            addr = self._collection_addresses.get(name[:-1], "")
            if addr:
                return addr
        name_lower = name.lower()
        for k, v in self._collection_addresses.items():
            if name_lower in k.lower() or k.lower() in name_lower:
                return v
        return ""

    # ── Floor prices ─────────────────────────────────────────────────────

    async def get_floor_prices_bulk(self, collection_names: list[str]) -> dict[str, int]:
        """Get floor prices for multiple collections.

        Returns dict: collection_name -> floor_price_nanoton.
        Uses gifts/collections/top for bulk, then collection/stats for rest.
        """
        floors: dict[str, int] = {}

        resp = await self._api_get("/v1/gifts/collections/top", {"limit": 100})
        if resp:
            for item in resp.get("items", []):
                coll = item.get("collection", {})
                name = coll.get("name", "")
                floor_ton = item.get("floorPrice", 0)
                if name and floor_ton and floor_ton > 0:
                    floors[name] = int(float(floor_ton) * NANO)

        await self.load_gift_collections()
        missing = [
            n for n in collection_names[:50] if n not in floors and n in self._collection_addresses
        ]

        for name in missing:
            addr = self._collection_addresses[name]
            floor = await self._get_collection_floor_by_addr(addr)
            if floor > 0:
                floors[name] = floor
            await asyncio.sleep(1.0)  # rate limit

        logger.info("Getgems: loaded floor prices for %d collections", len(floors))
        return floors

    async def _get_collection_floor_by_addr(self, address: str) -> int:
        """Get floor price for a collection by address. Returns nanoTON."""
        resp = await self._api_get(f"/v1/collection/stats/{address}")
        if not resp:
            return 0

        nano_str = resp.get("floorPriceNano", "0")
        try:
            return int(nano_str)
        except (ValueError, TypeError):
            floor_ton = resp.get("floorPrice", 0)
            return int(float(floor_ton) * NANO) if floor_ton else 0

    async def get_collection_floor(self, collection_name: str) -> int:
        """Get floor price for a specific collection in nanoTON."""
        await self.load_gift_collections()
        addr = self._collection_addresses.get(collection_name)
        if not addr:
            return 0
        return await self._get_collection_floor_by_addr(addr)

    # ── Gift listings ────────────────────────────────────────────────────

    async def get_gift_listings(
        self,
        collection_address: str = "",
        count: int = 50,
        sort_asc: bool = True,
        cursor: str | None = None,
        collection_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get gift listings on sale from Getgems via /v1/nfts/on-sale/{address}."""
        if collection_name and not collection_address:
            await self.load_gift_collections()
            collection_address = self._collection_addresses.get(collection_name, "")

        if not collection_address:
            return []

        params: dict[str, Any] = {"limit": min(count, 100)}
        if cursor:
            params["cursor"] = cursor

        resp = await self._api_get(f"/v1/nfts/on-sale/{collection_address}", params)

        if not resp:
            return []

        items = resp.get("items", [])
        results = []

        for item in items:
            name = item.get("name", "")
            parts = name.rsplit(" #", 1)
            coll_name = parts[0] if len(parts) > 1 else name

            sale = item.get("sale", {})
            price_raw = sale.get("fullPrice", "0") if sale else "0"
            # For auctions, use minBid
            if sale and sale.get("type") == "Auction":
                price_raw = sale.get("minBid", price_raw)

            try:
                price_nano = int(price_raw)
            except (ValueError, TypeError):
                price_nano = 0

            nft_addr = item.get("address", "")
            coll_addr = item.get("collectionAddress", "")

            results.append(
                {
                    "name": name,
                    "collection": coll_name,
                    "price_nanoton": price_nano,
                    "price_ton": price_nano / NANO if price_nano else 0,
                    "nft_address": nft_addr,
                    "collection_address": coll_addr,
                    "sale_type": sale.get("type", "unknown") if sale else "unknown",
                }
            )

        return results

    # ── Gift history ─────────────────────────────────────────────────────

    async def get_gifts_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """Get recent gift sales history."""
        resp = await self._api_get("/v1/nfts/history/gifts", {"limit": limit})
        if not resp:
            return []
        return resp.get("items", [])

    # ── Auto-listing on Getgems (requires Playwright CDP) ────────────────

    async def _get_browser(self) -> Any:
        """Get or create Playwright browser instance connected via CDP."""
        if self._browser and self._browser.is_connected():
            return self._browser

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.warning("Playwright not installed — auto-listing unavailable")
            return None

        if not self._pw:
            self._pw = await async_playwright().start()

        try:
            self._browser = await self._pw.chromium.connect_over_cdp(self._cdp_url)
            return self._browser
        except Exception as e:
            logger.error("Failed to connect to browser via CDP: %s", e)
            return None

    async def _apollo_mutate(self, mutation_str: str, variables: dict) -> dict | None:
        browser = await self._get_browser()
        if not browser:
            return None

        try:
            # Use the first available context
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            pages = ctx.pages
            page = None
            for pg in pages:
                if "getgems.io" in pg.url:
                    page = pg
                    break

            if page is None:
                page = await ctx.new_page()
                try:
                    await page.goto(
                        "https://getgems.io/gifts-collection",
                        wait_until="networkidle",
                        timeout=30000,
                    )
                except Exception as e:
                    logger.debug("Initial page load timeout: %s", e)

            # Ensure we are on a getgems page if somehow redirect happened
            if "getgems.io" not in page.url:
                 await page.goto("https://getgems.io/gifts-collection", wait_until="commit")

            # Wait for Apollo client to be ready on the page
            for _ in range(15):
                has_client = await page.evaluate("() => !!window.___xClient")
                if has_client:
                    break
                await asyncio.sleep(1)
            else:
                logger.error("Getgems: Apollo client (___xClient) not found on page")
                return None

            js_code = """
                async ([mutationStr, variables]) => {
                    try {
                        const { gql } = await import(
                            'https://esm.sh/@apollo/client@4.1.9/core'
                        ).catch(() => ({ gql: null }));

                        if (!gql) throw new Error("Could not load Apollo client from esm.sh");

                        const client = window.___xClient;
                        if (!client) throw new Error("window.___xClient not found");

                        const mutation = gql(mutationStr);
                        const result = await client.mutate({ mutation, variables });
                        return result.data;
                    } catch (e) {
                        return { __error: e.message };
                    }
                }
            """
            result = await page.evaluate(js_code, [mutation_str, variables])

            if result and "__error" in result:
                logger.error("Apollo mutation JS error: %s", result["__error"])
                return None

            return result
        except Exception as e:
            logger.error("Apollo mutate failed: %s", e)
            # Force browser reconnect on next try if it looks like a connection issue
            if "Target closed" in str(e) or "Browser closed" in str(e):
                self._browser = None
            return None

    async def authenticate(self, mnemonic: str) -> bool:
        """Authenticate with Getgems using TON Connect wallet."""
        try:
            from nacl.signing import SigningKey
        except ImportError:
            logger.error("pynacl not installed — cannot authenticate")
            return False

        try:
            words = mnemonic.strip().split()
            if len(words) != 24:
                logger.error("Invalid mnemonic: expected 24 words, got %d", len(words))
                return False

            from bot.ton_crypto import derive_keypair, get_wallet_address

            secret_key, public_key = derive_keypair(words)
            self._keypair = SigningKey(secret_key[:32])
            self._wallet_address = get_wallet_address(public_key)

            payload_data = await self._apollo_mutate(
                """mutation { generateTonConnectPayload { payload } }""",
                {},
            )
            if not payload_data:
                logger.error("Failed to generate TON Connect payload")
                return False
            payload = payload_data["generateTonConnectPayload"]["payload"]

            timestamp = int(time.time())
            domain = "getgems.io"
            proof = self._create_ton_proof(
                payload,
                timestamp,
                domain,
                public_key,
                secret_key,
            )

            from bot.ton_crypto import create_state_init

            state_init = create_state_init(public_key)

            login_data = await self._apollo_mutate(
                """mutation loginTonConnect($payload: TonConnectLoginInput!) {
                    loginTonConnect(payload: $payload) { token }
                }""",
                {
                    "payload": {
                        "accountId": self._wallet_address,
                        "tonProof": {
                            "timestamp": timestamp,
                            "domain": {"value": domain, "lengthBytes": len(domain)},
                            "signature": proof["signature"],
                            "payload": payload,
                            "stateInit": state_init,
                        },
                    }
                },
            )
            if not login_data:
                logger.error("loginTonConnect failed")
                return False

            self._auth_token = login_data["loginTonConnect"]["token"]
            logger.info("Getgems authenticated: %s", self._wallet_address)
            return True

        except Exception as e:
            logger.exception("Getgems authentication failed: %s", e)
            return False

    def _create_ton_proof(
        self,
        payload: str,
        timestamp: int,
        domain: str,
        public_key: bytes,
        secret_key: bytes,
    ) -> dict[str, str]:
        """Create ton_proof signature."""
        import base64

        from nacl.signing import SigningKey

        addr_parts = self._wallet_address.split(":")  # type: ignore[union-attr]
        workchain = int(addr_parts[0])
        addr_hash = bytes.fromhex(addr_parts[1])

        domain_bytes = domain.encode("utf-8")
        domain_len = len(domain_bytes)
        payload_bytes = payload.encode("utf-8")
        ts_bytes = struct.pack("<Q", timestamp)

        message = b"".join(
            [
                b"ton-proof-item-v2/",
                struct.pack("<i", workchain),
                addr_hash,
                struct.pack("<I", domain_len),
                domain_bytes,
                ts_bytes,
                payload_bytes,
            ]
        )

        msg_hash = hashlib.sha256(message).digest()
        full_msg = b"\xff\xff" + b"ton-connect" + msg_hash
        final_hash = hashlib.sha256(full_msg).digest()

        sk = SigningKey(secret_key[:32])
        signed = sk.sign(final_hash)
        signature = base64.b64encode(signed.signature).decode()

        return {"signature": signature}

    async def list_offchain_gift_rest(
        self,
        nft_address: str,
        price_nanoton: int,
    ) -> bool:
        """List an offchain gift for sale via Getgems GraphQL API.

        3-step flow:
        1. offchainNftCreateSingData → get sign text
        2. Sign text with wallet key (signData TIP-137)
        3. offchainNftPutOnSale → confirm with signature
        """
        if not self._keypair or not self._wallet_address:
            logger.error("Not authenticated — call authenticate_rest() first")
            return False

        # Refresh GraphQL token before each listing (tokens expire)
        if self._public_key and self._secret_key:
            from bot.ton_crypto import create_state_init

            si = create_state_init(self._public_key, self._secret_key)
            await self._authenticate_graphql(
                self._public_key,
                self._secret_key,
                si,
            )

        auth_token = self._graphql_token or self._user_token or ""
        if not auth_token:
            logger.error("No user token for Getgems GraphQL")
            return False

        gql_url = "https://getgems.io/graphql/"
        gql_headers = {
            "Accept": "application/graphql-response+json,application/json;q=0.9",
            "Content-Type": "application/json",
            "Origin": "https://getgems.io",
            "Referer": "https://getgems.io/",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/18.0 Safari/605.1.15"
            ),
            "x-auth-token": auth_token,
            "x-gg-client": "v:1 l:ru s:mpj0ui4q",
            "Cookie": f"AUTH_TOKEN={auth_token}",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        }

        gql_session = aiohttp.ClientSession(
            headers=gql_headers,
            timeout=aiohttp.ClientTimeout(total=30),
        )

        try:
            # Step 1: offchainNftCreateSingData — get sign text
            step1_body = {
                "operationName": "offchainNftCreateSingData",
                "variables": {
                    "nftAddress": nft_address,
                    "action": {
                        "fixPrice": {
                            "fullPriceNano": str(price_nanoton),
                            "currency": "TON",
                        }
                    },
                },
                "extensions": {
                    "clientLibrary": {"name": "@apollo/client", "version": "4.1.9"},
                    "persistedQuery": {
                        "version": 1,
                        "sha256Hash": (
                            "2192b01e127155a3c18a53e3600b8f4b10ac1364b63157cc6451cb147b9f69cf"
                        ),
                    },
                },
            }
            logger.info(
                "GG GraphQL step1: nft=%s price=%s token=%s...%s",
                nft_address,
                price_nanoton,
                auth_token[:8] if auth_token else "none",
                auth_token[-6:] if auth_token else "",
            )
            async with gql_session.post(gql_url, json=step1_body) as resp:
                resp_text = await resp.text()
                if resp.status != 200:
                    logger.error("GG createSingData HTTP %d: %s", resp.status, resp_text[:300])
                    return False
                import json as _json

                data = _json.loads(resp_text)

            if "errors" in data:
                logger.error("GG createSingData errors: %s", data["errors"])
                return False

            sing_text = data.get("data", {}).get("action", {}).get("singText", "")
            if not sing_text:
                logger.error("GG createSingData: no singText in response: %s", resp_text[:300])
                return False

            logger.info("GG step1 OK: singText=%s", sing_text[:80])

            # Step 2: sign the text with wallet key
            ts = int(time.time())
            domain = "getgems.io"
            signature = self._sign_data(domain, ts, sing_text)

            # Step 3: offchainNftPutOnSale — confirm with signature
            step3_body = {
                "operationName": "offchainNftPutOnSale",
                "variables": {
                    "nftAddress": nft_address,
                    "lang": "ru",
                    "sale": {
                        "fixPrice": {
                            "fullPriceNano": str(price_nanoton),
                            "currency": "TON",
                            "sing": {
                                "domain": domain,
                                "signature": signature,
                                "text": sing_text,
                                "timestamp": ts,
                            },
                        }
                    },
                },
                "extensions": {
                    "clientLibrary": {"name": "@apollo/client", "version": "4.1.9"},
                    "persistedQuery": {
                        "version": 1,
                        "sha256Hash": (
                            "ef07569deb85dc5db5401fb2b69fa8c536d88d307b39bd2a95551cb4782e9d78"
                        ),
                    },
                },
            }
            async with gql_session.post(gql_url, json=step3_body) as resp:
                resp_text = await resp.text()
                if resp.status != 200:
                    logger.error("GG putOnSale HTTP %d: %s", resp.status, resp_text[:300])
                    return False
                import json as _json

                data = _json.loads(resp_text)

            if "errors" in data:
                logger.error("GG putOnSale errors: %s", data["errors"])
                return False

            logger.info(
                "Listed on Getgems (GraphQL): %s for %.2f TON",
                nft_address,
                price_nanoton / NANO,
            )
            return True

        except Exception as e:
            logger.exception("list_offchain_gift_rest failed: %s", e)
            return False
        finally:
            await gql_session.close()

    # ── Falling price (Dutch auction) listing ──────────────────────────────

    # Allowed decrease intervals in milliseconds
    FALLING_INTERVALS_MS = {
        300_000: "5m",
        600_000: "10m",
        1_800_000: "30m",
        3_600_000: "1h",
        10_800_000: "3h",
        21_600_000: "6h",
        43_200_000: "12h",
        86_400_000: "1d",
        172_800_000: "2d",
    }

    async def list_offchain_gift_falling_price(
        self,
        nft_address: str,
        start_price_nanoton: int,
        min_price_nanoton: int,
        decrease_value_nanoton: int,
        decrease_interval_ms: int = 3_600_000,
    ) -> bool:
        """List an offchain gift with falling price on Getgems.

        Same 3-step flow as fixed price, but using 'fallingPrice' action.

        Args:
            nft_address: NFT contract address.
            start_price_nanoton: Starting (highest) price in nanoTON.
            min_price_nanoton: Minimum (lowest) price in nanoTON.
            decrease_value_nanoton: Price decrease per interval in nanoTON.
            decrease_interval_ms: Interval between decreases in milliseconds.
                Allowed: 300000 (5m), 600000 (10m), 1800000 (30m),
                3600000 (1h), 10800000 (3h), 21600000 (6h),
                43200000 (12h), 86400000 (1d), 172800000 (2d).
        """
        if not self._keypair or not self._wallet_address:
            logger.error("Not authenticated — call authenticate_rest() first")
            return False

        if start_price_nanoton <= min_price_nanoton:
            logger.error(
                "Start price (%d) must be > min price (%d)",
                start_price_nanoton,
                min_price_nanoton,
            )
            return False

        if decrease_value_nanoton <= 0:
            logger.error("Decrease value must be positive: %d", decrease_value_nanoton)
            return False

        if (start_price_nanoton - min_price_nanoton) < decrease_value_nanoton:
            logger.error(
                "Price range (%d) must be >= decrease step (%d)",
                start_price_nanoton - min_price_nanoton,
                decrease_value_nanoton,
            )
            return False

        if decrease_interval_ms not in self.FALLING_INTERVALS_MS:
            logger.error("Invalid decrease interval: %d ms", decrease_interval_ms)
            return False

        # Refresh GraphQL token
        if self._public_key and self._secret_key:
            from bot.ton_crypto import create_state_init

            si = create_state_init(self._public_key, self._secret_key)
            await self._authenticate_graphql(
                self._public_key,
                self._secret_key,
                si,
            )

        auth_token = self._graphql_token or self._user_token or ""
        if not auth_token:
            logger.error("No user token for Getgems GraphQL")
            return False

        gql_url = "https://getgems.io/graphql/"
        gql_headers = {
            "Accept": "application/graphql-response+json,application/json;q=0.9",
            "Content-Type": "application/json",
            "Origin": "https://getgems.io",
            "Referer": "https://getgems.io/",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/18.0 Safari/605.1.15"
            ),
            "x-auth-token": auth_token,
            "x-gg-client": "v:1 l:ru s:mpj0ui4q",
            "Cookie": f"AUTH_TOKEN={auth_token}",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        }

        gql_session = aiohttp.ClientSession(
            headers=gql_headers,
            timeout=aiohttp.ClientTimeout(total=30),
        )

        try:
            falling_action = {
                "fallingPrice": {
                    "fullPriceNano": str(start_price_nanoton),
                    "minPriceNano": str(min_price_nanoton),
                    "decreaseValueNano": str(decrease_value_nanoton),
                    "decreaseInterval": str(decrease_interval_ms),
                    "currency": "TON",
                }
            }

            # Step 1: offchainNftCreateSingData
            step1_body = {
                "operationName": "offchainNftCreateSingData",
                "variables": {
                    "nftAddress": nft_address,
                    "action": falling_action,
                },
                "extensions": {
                    "clientLibrary": {"name": "@apollo/client", "version": "4.1.9"},
                    "persistedQuery": {
                        "version": 1,
                        "sha256Hash": (
                            "2192b01e127155a3c18a53e3600b8f4b10ac1364b63157cc6451cb147b9f69cf"
                        ),
                    },
                },
            }

            logger.info(
                "GG fallingPrice step1: nft=%s start=%s min=%s step=%s interval=%sms",
                nft_address,
                start_price_nanoton,
                min_price_nanoton,
                decrease_value_nanoton,
                decrease_interval_ms,
            )

            async with gql_session.post(gql_url, json=step1_body) as resp:
                resp_text = await resp.text()
                if resp.status != 200:
                    logger.error(
                        "GG fallingPrice createSingData HTTP %d: %s",
                        resp.status,
                        resp_text[:300],
                    )
                    return False
                import json as _json

                data = _json.loads(resp_text)

            if "errors" in data:
                logger.error("GG fallingPrice createSingData errors: %s", data["errors"])
                return False

            sing_text = data.get("data", {}).get("action", {}).get("singText", "")
            if not sing_text:
                logger.error(
                    "GG fallingPrice createSingData: no singText: %s", resp_text[:300]
                )
                return False

            logger.info("GG fallingPrice step1 OK: singText=%s", sing_text[:80])

            # Step 2: sign
            ts = int(time.time())
            domain = "getgems.io"
            signature = self._sign_data(domain, ts, sing_text)

            # Step 3: offchainNftPutOnSale with fallingPrice
            falling_sale = {
                "fallingPrice": {
                    "fullPriceNano": str(start_price_nanoton),
                    "minPriceNano": str(min_price_nanoton),
                    "decreaseValueNano": str(decrease_value_nanoton),
                    "decreaseInterval": str(decrease_interval_ms),
                    "currency": "TON",
                    "sing": {
                        "domain": domain,
                        "signature": signature,
                        "text": sing_text,
                        "timestamp": ts,
                    },
                }
            }

            step3_body = {
                "operationName": "offchainNftPutOnSale",
                "variables": {
                    "nftAddress": nft_address,
                    "lang": "ru",
                    "sale": falling_sale,
                },
                "extensions": {
                    "clientLibrary": {"name": "@apollo/client", "version": "4.1.9"},
                    "persistedQuery": {
                        "version": 1,
                        "sha256Hash": (
                            "ef07569deb85dc5db5401fb2b69fa8c536d88d307b39bd2a95551cb4782e9d78"
                        ),
                    },
                },
            }

            async with gql_session.post(gql_url, json=step3_body) as resp:
                resp_text = await resp.text()
                if resp.status != 200:
                    logger.error(
                        "GG fallingPrice putOnSale HTTP %d: %s",
                        resp.status,
                        resp_text[:300],
                    )
                    return False
                import json as _json

                data = _json.loads(resp_text)

            if "errors" in data:
                logger.error("GG fallingPrice putOnSale errors: %s", data["errors"])
                return False

            interval_label = self.FALLING_INTERVALS_MS.get(
                decrease_interval_ms, f"{decrease_interval_ms}ms"
            )
            logger.info(
                "Listed on Getgems (FallingPrice): %s "
                "start=%.2f→min=%.2f TON, step=%.2f, every %s",
                nft_address,
                start_price_nanoton / NANO,
                min_price_nanoton / NANO,
                decrease_value_nanoton / NANO,
                interval_label,
            )
            return True

        except Exception as e:
            logger.exception("list_offchain_gift_falling_price failed: %s", e)
            return False
        finally:
            await gql_session.close()

    # ── Buy on Getgems ────────────────────────────────────────────────────

    async def buy_gift(self, nft_address: str, sale_version: str) -> bool:
        """Buy an offchain gift on Getgems.

        1. POST buy-fix-price → get TON transaction details
        2. Build wallet transfer with payload
        3. Send transaction via toncenter API
        """
        if not self._keypair or not self._wallet_address:
            logger.error("Not authenticated — call authenticate_rest() first")
            return False

        session = await self._get_session()
        try:
            # Step 1: get buy transaction
            async with session.post(
                f"{PUBLIC_API_BASE}/v1/nfts/buy-fix-price/{nft_address}",
                json={"version": sale_version},
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error("buy-fix-price %d: %s", resp.status, text[:300])
                    return False
                data = await resp.json()
                if not data.get("success"):
                    logger.error("buy-fix-price failed: %s", data)
                    return False

            tx = data["response"]
            tx_list = tx.get("list", [])
            if not tx_list:
                logger.error("buy-fix-price returned empty transaction list")
                return False

            # Capture pre-buy balance for verification
            try:
                pre_balance = await self.get_wallet_balance()
            except Exception:
                pre_balance = 0

            # Step 2: send TON transaction(s)
            for tx_item in tx_list:
                to_addr = tx_item["to"]
                amount = int(tx_item["amount"])
                payload_b64 = tx_item.get("payload")
                state_init_b64 = tx_item.get("stateInit")

                success = await self._send_ton_transfer(
                    to_addr,
                    amount,
                    payload_b64,
                    state_init_b64,
                )
                if not success:
                    logger.error("TON transfer failed for buy %s", nft_address)
                    return False

            buy_amount = int(tx_list[0]["amount"])
            logger.info(
                "TON sent for Getgems buy: %s for %.2f TON — verifying...",
                nft_address,
                buy_amount / NANO,
            )

            # Step 3: verify purchase by checking if balance decreased
            await asyncio.sleep(8)
            try:
                new_balance = await self.get_wallet_balance()
                balance_dropped = (pre_balance - new_balance) > buy_amount * 0.5
                if not balance_dropped:
                    logger.warning(
                        "Buy NOT confirmed: balance before=%.2f after=%.2f "
                        "(expected drop of %.2f TON) — gift likely already sold",
                        pre_balance / NANO,
                        new_balance / NANO,
                        buy_amount / NANO,
                    )
                    return False
            except Exception as e:
                logger.debug("Post-buy balance check failed: %s", e)
                # Can't verify — assume success (broadcast went through)

            logger.info(
                "Bought on Getgems: %s for %.2f TON",
                nft_address,
                buy_amount / NANO,
            )
            return True

        except Exception as e:
            logger.exception("buy_gift failed: %s", e)
            return False

    async def _send_ton_transfer(
        self,
        to_address: str,
        amount: int,
        payload_b64: str | None = None,
        state_init_b64: str | None = None,
    ) -> bool:
        """Send a TON transfer from the wallet."""
        import base64

        from tonsdk.boc import Cell
        from tonsdk.utils import Address

        try:
            # Get wallet seqno
            seqno = await self._get_wallet_seqno()
            if seqno is None:
                logger.error("Failed to get wallet seqno")
                return False

            # Build transfer message
            from tonsdk.contract.wallet import WalletV4ContractR2

            wallet = WalletV4ContractR2(
                public_key=self._public_key,
                private_key=self._secret_key,
            )

            # Parse destination address — keep bounceable flag from API
            dest = Address(to_address)
            addr_str = dest.to_string(True, True, dest.is_bounceable)

            # Parse payload cell if present
            body = Cell()
            if payload_b64:
                body = Cell.one_from_boc(base64.b64decode(payload_b64))

            # Parse state init if present
            si = None
            if state_init_b64:
                si = Cell.one_from_boc(base64.b64decode(state_init_b64))

            # Create transfer
            transfer = wallet.create_transfer_message(
                to_addr=addr_str,
                amount=amount,
                seqno=seqno,
                payload=body,
                state_init=si,
            )

            # Serialize to BOC
            boc = bytes(transfer["message"].to_boc(False))
            boc_b64 = base64.b64encode(boc).decode()

            # Send via toncenter
            success = await self._broadcast_boc(boc_b64)
            return success

        except Exception as e:
            logger.exception("_send_ton_transfer failed: %s", e)
            return False

    async def _get_chain_session(self) -> aiohttp.ClientSession:
        """Separate session for blockchain calls (no Getgems auth headers)."""
        if self._chain_session is None or self._chain_session.closed:
            self._chain_session = aiohttp.ClientSession(
                headers={"Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._chain_session

    async def _get_wallet_seqno(self) -> int | None:
        """Get wallet seqno from TON blockchain."""
        if not self._wallet_address:
            return None

        from tonsdk.utils import Address

        addr_parts = self._wallet_address.split(":")
        addr = Address(f"0:{addr_parts[1]}")
        friendly = addr.to_string(True, True, False)

        session = await self._get_chain_session()

        # Try tonapi first (more reliable for this wallet)
        try:
            async with session.get(
                f"https://tonapi.io/v2/wallet/{friendly}/seqno",
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    seqno = data.get("seqno", 0)
                    logger.debug("tonapi seqno=%d", seqno)
                    return seqno
                logger.debug("tonapi seqno %d", resp.status)
        except Exception as e:
            logger.debug("tonapi seqno error: %s", e)

        # Fallback: toncenter
        for attempt in range(3):
            try:
                async with session.get(
                    "https://toncenter.com/api/v2/runGetMethod",
                    params={"address": friendly, "method": "seqno", "stack": "[]"},
                ) as resp:
                    if resp.status == 429:
                        logger.warning("toncenter seqno 429, retry %d", attempt)
                        await asyncio.sleep(2 * (attempt + 1))
                        continue
                    if resp.status not in (200, 404):
                        text = await resp.text()
                        logger.debug("toncenter seqno %d: %s", resp.status, text[:200])
                        break
                    if resp.status == 404:
                        logger.info("Wallet not deployed (toncenter 404), seqno=0")
                        return 0
                    data = await resp.json()
                    result = data.get("result", {})
                    stack = result.get("stack", [])
                    if stack and len(stack) > 0:
                        val = stack[0]
                        if isinstance(val, list) and len(val) == 2:
                            return int(val[1], 16)
                        elif isinstance(val, str):
                            return int(val, 16)
                    logger.warning("No seqno in toncenter result: %s", data)
                    return 0
            except Exception as e:
                logger.debug("toncenter seqno error: %s", e)
                await asyncio.sleep(1)

        return None

    async def _broadcast_boc(self, boc_b64: str) -> bool:
        """Broadcast a signed BOC to TON blockchain with retry."""
        endpoints = [
            "https://toncenter.com/api/v2/sendBoc",
            "https://tonapi.io/v2/blockchain/message",
        ]
        session = await self._get_chain_session()

        for attempt in range(3):
            for url in endpoints:
                try:
                    is_tonapi = "tonapi" in url
                    if is_tonapi:
                        payload = {"boc": boc_b64}
                        async with session.post(
                            url, json=payload,
                        ) as resp:
                            if resp.status == 200:
                                logger.info("TON broadcast OK via tonapi")
                                return True
                            text = await resp.text()
                            logger.debug("tonapi %d: %s", resp.status, text[:200])
                    else:
                        async with session.post(
                            url, json={"boc": boc_b64},
                        ) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                if data.get("ok"):
                                    logger.info("TON broadcast OK via toncenter")
                                    return True
                                logger.debug("toncenter error: %s", data)
                            elif resp.status == 429:
                                logger.warning("toncenter 429, retrying...")
                            else:
                                text = await resp.text()
                                logger.debug("toncenter %d: %s", resp.status, text[:200])
                except Exception as e:
                    logger.debug("Broadcast attempt failed: %s", e)

            if attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))

        logger.error("TON broadcast failed after 3 attempts")
        return False

    async def get_wallet_balance(self) -> int:
        """Get wallet TON balance in nanoTON. Returns 0 on error."""
        if not self._wallet_address:
            return 0
        from tonsdk.utils import Address

        addr_parts = self._wallet_address.split(":")
        addr = Address(f"0:{addr_parts[1]}")
        friendly = addr.to_string(True, True, False)

        session = await self._get_chain_session()
        try:
            async with session.get(
                "https://toncenter.com/api/v2/getAddressBalance",
                params={"address": friendly},
            ) as resp:
                if resp.status != 200:
                    return 0
                data = await resp.json()
                return int(data.get("result", "0"))
        except Exception as e:
            logger.debug("get_wallet_balance error: %s", e)
            return 0

    async def get_on_sale_gifts(
        self,
        collection_address: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Get gifts on sale in a specific collection (with sale details)."""
        resp = await self._api_get(
            f"/v1/nfts/offchain/on-sale/{collection_address}",
            {"limit": limit},
        )
        if resp:
            return resp.get("items", [])
        return []

    async def authenticate_rest(self, mnemonic: str) -> bool:
        """Authenticate with wallet for REST API operations.

        1. Derives keypair + wallet address from mnemonic
        2. Creates ton-proof and authenticates via /auth/ton-proof
        3. Stores user auth token for user-specific API calls (e.g. list gifts)
        """
        try:
            from nacl.signing import SigningKey
        except ImportError:
            logger.error("pynacl not installed — pip install pynacl")
            return False

        try:
            words = mnemonic.strip().split()
            if len(words) != 24:
                logger.error("Invalid mnemonic: expected 24 words, got %d", len(words))
                return False

            from bot.ton_crypto import create_state_init, derive_keypair, get_wallet_address

            secret_key, public_key = derive_keypair(words)
            self._keypair = SigningKey(secret_key[:32])
            self._wallet_address = get_wallet_address(public_key, secret_key)
            self._secret_key = secret_key
            self._public_key = public_key
            logger.info("Getgems wallet ready: %s", self._wallet_address)

            # Authenticate via ton-proof to get user token
            timestamp = int(time.time())
            domain = "getgems.io"
            payload = "getgems-llm"
            proof = self._create_ton_proof(payload, timestamp, domain, public_key, secret_key)
            state_init = create_state_init(public_key, secret_key)

            session = await self._get_session()
            auth_body = {
                "address": self._wallet_address,
                "chain": "-239",
                "walletStateInit": state_init,
                "publicKey": public_key.hex(),
                "timestamp": timestamp,
                "domainLengthBytes": len(domain.encode("utf-8")),
                "domainValue": domain,
                "signature": proof["signature"],
                "payload": payload,
                "authApplication": "devin-mrkt-bot",
            }
            async with session.post(
                f"{PUBLIC_API_BASE}/auth/ton-proof",
                json=auth_body,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    token = data.get("response", {}).get("token") or data.get("token", "")
                    if token:
                        self._user_token = token
                        await self._reset_session()
                        logger.info("Getgems user auth OK (ton-proof)")
                    else:
                        logger.warning("Getgems ton-proof returned no token: %s", data)
                else:
                    text = await resp.text()
                    logger.warning("Getgems ton-proof auth %d: %s", resp.status, text[:300])

            # Also authenticate via GraphQL loginTonConnect for website operations
            await self._authenticate_graphql(
                public_key,
                secret_key,
                state_init,
            )

            return True

        except Exception as e:
            logger.exception("authenticate_rest failed: %s", e)
            return False

    async def _authenticate_graphql(
        self,
        public_key: bytes,
        secret_key: bytes,
        state_init: str,
    ) -> bool:
        """Authenticate via Getgems website GraphQL (loginTonConnect).

        3-step flow:
        1. createLoginMessage → server-issued challenge nonce
        2. Sign ton-proof with that challenge as payload
        3. loginTonConnect → website AUTH_TOKEN for put-on-sale mutations
        """
        try:
            gql_url = "https://getgems.io/graphql/"
            gql_headers = {
                "Accept": "application/graphql-response+json,application/json;q=0.9",
                "Content-Type": "application/json",
                "Origin": "https://getgems.io",
                "Referer": "https://getgems.io/",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                    "Version/18.0 Safari/605.1.15"
                ),
                "x-gg-client": "v:1 l:ru s:mpj0ui4q",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Dest": "empty",
            }

            import hashlib as _hl
            import json as _json

            async with aiohttp.ClientSession(
                headers=gql_headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as sess:
                # Step 1: Get challenge nonce
                challenge_q = "mutation createLoginMessage { createLoginMessage }"
                challenge_h = _hl.sha256(challenge_q.encode()).hexdigest()
                body1 = {
                    "operationName": "createLoginMessage",
                    "query": challenge_q,
                    "variables": {},
                    "extensions": {
                        "persistedQuery": {"version": 1, "sha256Hash": challenge_h},
                    },
                }
                async with sess.post(gql_url, json=body1) as resp:
                    data1 = _json.loads(await resp.text())

                challenge = data1.get("data", {}).get("createLoginMessage", "")
                if not challenge:
                    logger.error("GG createLoginMessage failed: %s", data1)
                    return False
                logger.info("GG login challenge: %s", challenge[:20])

                # Step 2: Sign ton-proof with challenge
                domain = "getgems.io"
                timestamp = int(time.time())
                proof = self._create_ton_proof(
                    challenge,
                    timestamp,
                    domain,
                    public_key,
                    secret_key,
                )

                # Step 3: loginTonConnect
                login_q = (
                    "mutation loginTonConnect($payload: TonConnectAuthPayload!) "
                    "{ loginTonConnect(payload: $payload) { token } }"
                )
                login_h = _hl.sha256(login_q.encode()).hexdigest()
                addr = self._wallet_address or ""
                body3 = {
                    "operationName": "loginTonConnect",
                    "query": login_q,
                    "variables": {
                        "payload": {
                            "address": addr,
                            "chain": "-239",
                            "walletStateInit": state_init,
                            "publicKey": public_key.hex(),
                            "timestamp": float(timestamp),
                            "domainLengthBytes": len(domain.encode("utf-8")),
                            "domainValue": domain,
                            "signature": proof["signature"],
                            "payload": challenge,
                            "authApplication": "TonConnect",
                        },
                    },
                    "extensions": {
                        "clientLibrary": {
                            "name": "@apollo/client",
                            "version": "4.1.9",
                        },
                        "persistedQuery": {
                            "version": 1,
                            "sha256Hash": login_h,
                        },
                    },
                }
                async with sess.post(gql_url, json=body3) as resp:
                    resp_text = await resp.text()
                    if resp.status != 200:
                        logger.error(
                            "GG loginTonConnect HTTP %d: %s",
                            resp.status,
                            resp_text[:300],
                        )
                        return False
                    data3 = _json.loads(resp_text)

            if "errors" in data3:
                logger.error("GG loginTonConnect errors: %s", data3["errors"])
                return False

            token = data3.get("data", {}).get("loginTonConnect", {}).get("token", "")
            if token:
                self._graphql_token = token
                logger.info(
                    "Getgems GraphQL auth OK: token=%s...%s",
                    token[:8],
                    token[-6:],
                )
                return True

            logger.warning(
                "GG loginTonConnect: no token in response: %s",
                resp_text[:300],
            )
            return False

        except Exception as e:
            logger.exception("_authenticate_graphql failed: %s", e)
            return False

    async def _api_get_user(
        self, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """GET request with user auth token (ton-proof)."""
        token = self._user_token
        if not token:
            return await self._api_get(path, params)

        session = await self._get_session()
        url = f"{PUBLIC_API_BASE}{path}"
        headers = {"Authorization": f"Bearer {token}"}

        for attempt in range(3):
            try:
                async with session.get(url, params=params, headers=headers) as resp:
                    if resp.status == 429:
                        await asyncio.sleep(min(5.0 * (attempt + 1), 30.0))
                        continue
                    if resp.status != 200:
                        text = await resp.text()
                        logger.info("Getgems user API %d on %s: %s", resp.status, path, text[:200])
                        return None
                    data = await resp.json()
                    if not data.get("success"):
                        return None
                    return data.get("response")
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.error("Getgems user request error on %s: %s", path, e)
                await asyncio.sleep(2.0)
        return None

    async def get_user_offchain_gifts(self, owner_address: str) -> list[dict[str, Any]]:
        """Get user's offchain gifts via /v1/nfts/owner (works with GGLLM token)."""
        logger.info(
            "get_user_offchain_gifts: user_token=%s, addr=%s...%s",
            bool(self._user_token),
            owner_address[:10],
            owner_address[-6:],
        )

        if self._user_token:
            resp = await self._api_get_user(
                f"/v1/nfts/owner/{owner_address}",
            )
            if resp:
                items = resp.get("items", [])
                logger.info("Found %d NFTs for owner (user token)", len(items))
                return items
            else:
                logger.info("User token endpoint returned no data, trying fallback")

        # Fallback: search each known collection for user's items
        results: list[dict[str, Any]] = []
        if not self._collection_addresses:
            await self.load_gift_collections()
        n_colls = len(self._collection_addresses)
        logger.info("Fallback: scanning %d collections for user gifts", n_colls)
        for name, addr in list(self._collection_addresses.items()):
            try:
                resp = await self._api_get(
                    f"/v1/nfts/collection/{addr}/owner/{owner_address}",
                    {"limit": 20},
                )
                if resp:
                    items = resp.get("items", [])
                    if items:
                        logger.info("Found %d gifts in collection %s", len(items), name)
                    results.extend(items)
            except Exception:
                pass
            await asyncio.sleep(0.3)
        logger.info("Fallback scan complete: %d total gifts found", len(results))
        return results

    async def find_gift_in_collection(
        self,
        collection_name: str,
        gift_number: int | str,
    ) -> dict[str, Any] | None:
        """Find a specific gift by collection name and number on Getgems.

        Searches the on-sale list for the gift. Returns the item dict or None.
        """
        if not self._collection_addresses:
            await self.load_gift_collections()

        # Try exact name and plural forms
        addr = self._collection_addresses.get(collection_name, "")
        if not addr:
            addr = self._collection_addresses.get(collection_name + "s", "")
        if not addr:
            addr = self._collection_addresses.get(collection_name + "es", "")
        if not addr:
            for k, v in self._collection_addresses.items():
                if collection_name.lower() in k.lower() or k.lower() in collection_name.lower():
                    addr = v
                    break

        if not addr:
            logger.debug("Collection %s not found on Getgems", collection_name)
            return None

        # Search on-sale items in this collection
        target = f"#{gift_number}"
        cursor: str | None = None
        for _ in range(10):
            params: dict[str, Any] = {"limit": 50}
            if cursor:
                params["cursor"] = cursor
            resp = await self._api_get(
                f"/v1/nfts/offchain/on-sale/{addr}",
                params,
            )
            if not resp:
                break
            items = resp.get("items", [])
            for item in items:
                name = item.get("name", "")
                if target in name:
                    return item
            cursor = resp.get("cursor")
            if not cursor or not items:
                break
            await asyncio.sleep(1)

        return None

    async def list_gift_for_sale(
        self,
        nft_address: str,
        price_nanoton: int,
    ) -> bool:
        """List a gift for sale on Getgems using signData."""
        if not self._auth_token or not self._keypair:
            logger.error("Not authenticated — call authenticate() first")
            return False

        try:
            cart_data = await self._apollo_mutate(
                """mutation calculateCart($input: CartInput!) {
                    calculateCart(input: $input) {
                        id items { id } totalPrice
                    }
                }""",
                {
                    "input": {
                        "items": [
                            {
                                "nftAddress": nft_address,
                                "putUpForSaleNft": {
                                    "fullPrice": str(price_nanoton),
                                    "marketplaceFee": str(int(price_nanoton * 0.05)),
                                    "royaltyAmount": "0",
                                },
                            }
                        ],
                    }
                },
            )
            if not cart_data:
                logger.error("calculateCart failed")
                return False

            cart_id = cart_data["calculateCart"]["id"]

            tx_data = await self._apollo_mutate(
                """mutation createCartTx($input: CreateCartTxInput!) {
                    createCartTx(input: $input) {
                        tx { validUntil messages { address amount payload } }
                        signData { domain timestamp payload }
                        errors { message }
                    }
                }""",
                {
                    "input": {
                        "id": cart_id,
                        "putUpForSaleNft": {
                            "fullPrice": str(price_nanoton),
                            "marketplaceFee": str(int(price_nanoton * 0.05)),
                            "royaltyAmount": "0",
                        },
                    }
                },
            )
            if not tx_data:
                logger.error("createCartTx failed")
                return False

            cart_tx = tx_data["createCartTx"]
            errors = cart_tx.get("errors", [])
            if errors:
                logger.error("createCartTx errors: %s", errors)
                return False

            sign_data = cart_tx.get("signData")
            if not sign_data:
                logger.error("No signData in createCartTx response")
                return False

            domain_str = sign_data["domain"]
            ts = sign_data["timestamp"]
            payload_text = sign_data["payload"]
            signature = self._sign_data(domain_str, ts, payload_text)

            exec_data = await self._apollo_mutate(
                """mutation executeSignCart($input: ExecuteSignCartInput!) {
                    executeSignCart(input: $input) {
                        errors { message }
                    }
                }""",
                {
                    "input": {
                        "id": cart_id,
                        "signData": {
                            "signature": signature,
                            "timestamp": str(ts),
                            "domain": domain_str,
                            "payload": payload_text,
                            "address": self._wallet_address,
                            "publicKey": self._keypair.verify_key.encode().hex(),
                        },
                    }
                },
            )
            if not exec_data:
                logger.error("executeSignCart failed")
                return False

            exec_errors = exec_data["executeSignCart"].get("errors", [])
            if exec_errors:
                logger.error("executeSignCart errors: %s", exec_errors)
                return False

            logger.info(
                "Listed on Getgems: %s for %.2f TON",
                nft_address,
                price_nanoton / NANO,
            )
            return True

        except Exception as e:
            logger.exception("list_gift_for_sale failed: %s", e)
            return False

    def _sign_data(self, domain: str, timestamp: int, payload: str) -> str:
        """Sign text using TON Connect signData (TIP-137 Big-Endian)."""
        import base64

        addr_parts = self._wallet_address.split(":")  # type: ignore[union-attr]
        workchain = int(addr_parts[0])
        addr_hash = bytes.fromhex(addr_parts[1])

        domain_bytes = domain.encode("utf-8")
        payload_bytes = payload.encode("utf-8")

        message = b"".join(
            [
                b"\xff\xff",
                b"ton-connect/sign-data/",
                struct.pack(">i", workchain),
                addr_hash,
                struct.pack(">I", len(domain_bytes)),
                domain_bytes,
                struct.pack(">Q", timestamp),
                b"txt",
                struct.pack(">I", len(payload_bytes)),
                payload_bytes,
            ]
        )

        msg_hash = hashlib.sha256(message).digest()
        signed = self._keypair.sign(msg_hash)
        return base64.b64encode(signed.signature).decode()
