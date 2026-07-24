"""Fragment marketplace client — reads floor prices and searches gifts.

Uses pyfragment library for Fragment.com API access.
Provides floor price fetching for cross-market arbitrage with MRKT/Getgems.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import struct
import time
from collections.abc import Callable, Coroutine
from typing import Any

logger = logging.getLogger(__name__)

NANO = 1_000_000_000


def _name_to_slug(name: str) -> str:
    """Convert collection name to Fragment slug.

    'Clover Pin' -> 'cloverpin'
    'Flying Broom' -> 'flyingbroom'
    'Lush Bouquet' -> 'lushbouquet'
    """
    return re.sub(r"[^a-z0-9]", "", name.lower())


class FragmentClient:
    """Client for Fragment.com marketplace: floor prices + search."""

    def __init__(
        self,
        seed: str = "",
        api_key: str = "",
        cookies: dict[str, str] | None = None,
    ) -> None:
        self._seed = seed
        self._api_key = api_key
        self._cookies = cookies or {}
        self._client: Any = None  # pyfragment.FragmentClient
        self._initialized = False
        self._auth_alert_sent = False
        self._on_auth_expired: Callable[[], Coroutine[Any, Any, None]] | None = None

        # Cache: collection_name -> floor_price_nanoton
        self._floor_cache: dict[str, int] = {}
        self._last_bulk_fetch: float = 0
        self._slug_cache: dict[str, str] = {}  # name -> slug

    def set_auth_expired_callback(
        self, cb: Callable[[], Coroutine[Any, Any, None]]
    ) -> None:
        self._on_auth_expired = cb

    def update_cookies(self, cookies: dict[str, str]) -> None:
        """Update Fragment cookies at runtime and reinitialize."""
        self._cookies = cookies
        self._auth_alert_sent = False
        self._initialized = False
        self._client = None
        logger.info("Fragment cookies updated, will reinitialize on next use")

    async def initialize(self) -> bool:
        """Initialize the Fragment client. Returns True if successful."""
        if not self._seed or not self._api_key or not self._cookies:
            logger.warning("Fragment: missing credentials (seed/api_key/cookies)")
            return False

        try:
            from pyfragment import FragmentClient as PyFragmentClient

            self._client = PyFragmentClient(
                seed=self._seed,
                api_key=self._api_key,
                cookies=self._cookies,
                wallet_version="V4R2",
            )
            # Auto-connect wallet via TonConnect ton_proof
            await self._connect_wallet()

            # Test connection
            result = await self._client.search_gifts(
                sort="price_asc",
                filter="sale",
            )
            if result and result.items:
                logger.info(
                    "Fragment client initialized: %d gifts found on first page",
                    len(result.items),
                )
                self._initialized = True
                return True
            else:
                logger.warning("Fragment: search returned empty results")
                return False
        except Exception as e:
            logger.error("Fragment initialization failed: %s", e)
            return False

    async def _connect_wallet(self) -> None:
        """Authenticate wallet on Fragment via TonConnect ton_proof signing."""
        try:
            import httpx
            import nacl.signing
            from pyfragment.core.constants import BASE_HEADERS, WALLET_CLASSES
            from ton_core import NetworkGlobalID
            from tonutils.clients import TonapiClient

            # Get ton_proof challenge from Fragment HTML page
            html_headers = {
                "user-agent": BASE_HEADERS["user-agent"],
                "accept": "text/html,application/xhtml+xml",
                "accept-language": "en-US,en;q=0.9",
            }
            async with httpx.AsyncClient(cookies=self._cookies, timeout=15) as http:
                r = await http.get("https://fragment.com/", headers=html_headers)
                m = re.search(r'"ton_proof":"([^"]+)"', r.text)
                if not m:
                    logger.warning("Fragment: no ton_proof challenge found")
                    return
                proof_payload = m.group(1)
                m2 = re.search(r'"apiUrl":"[^"]*hash=([^"]+)"', r.text)
                api_hash = m2.group(1) if m2 else ""

                # Check if already logged in
                m3 = re.search(r'"logged_in":\s*(true|false)', r.text)
                if m3 and m3.group(1) == "true":
                    logger.info("Fragment: wallet already connected")
                    return

            # Build and sign ton_proof
            async with TonapiClient(network=NetworkGlobalID.MAINNET, api_key=self._api_key) as ton:
                wallet_cls = WALLET_CLASSES["V4R2"]
                wallet, pub_key, priv_key, _ = wallet_cls.from_mnemonic(
                    client=ton, mnemonic=self._seed
                )
                address_raw = wallet.address.to_str(False, False)
                parts = address_raw.split(":")
                wc = int(parts[0])
                addr_hash = bytes.fromhex(parts[1])
                state_init_boc = base64.b64encode(wallet.state_init.serialize().to_boc()).decode()

            timestamp = int(time.time())
            domain = "fragment.com"
            domain_bytes = domain.encode()

            msg = (
                b"ton-proof-item-v2/"
                + struct.pack("<i", wc)
                + addr_hash
                + struct.pack("<I", len(domain_bytes))
                + domain_bytes
                + struct.pack("<Q", timestamp)
                + proof_payload.encode()
            )
            msg_hash = hashlib.sha256(msg).digest()
            final_msg = hashlib.sha256(bytes.fromhex("ffff") + b"ton-connect" + msg_hash).digest()
            signing_key = nacl.signing.SigningKey(priv_key.as_bytes)
            signature = base64.b64encode(signing_key.sign(final_msg).signature).decode()

            account_obj = {
                "address": address_raw,
                "publicKey": pub_key.as_hex,
                "chain": "-239",
                "walletStateInit": state_init_boc,
            }
            device_obj = {
                "platform": "linux",
                "appName": "fragment-bot",
                "appVersion": "1.0",
                "maxProtocolVersion": 2,
                "features": [{"name": "SendTransaction", "maxMessages": 4}],
            }
            proof_obj = {
                "timestamp": timestamp,
                "domain": {
                    "lengthBytes": len(domain_bytes),
                    "value": domain,
                },
                "signature": signature,
                "payload": proof_payload,
            }

            async with httpx.AsyncClient(cookies=self._cookies, timeout=15) as http:
                resp = await http.post(
                    f"https://fragment.com/api?hash={api_hash}",
                    data={
                        "method": "checkTonProofAuth",
                        "account": json.dumps(account_obj),
                        "device": json.dumps(device_obj),
                        "proof": json.dumps(proof_obj),
                    },
                    headers={
                        **BASE_HEADERS,
                        "referer": "https://fragment.com/",
                        "x-aj-referer": "https://fragment.com/",
                    },
                )
                result = resp.json()
                if result.get("verified"):
                    new_token = resp.cookies.get("stel_ton_token")
                    if new_token:
                        self._cookies["stel_ton_token"] = new_token
                        self._client.cookies["stel_ton_token"] = new_token
                    logger.info("Fragment: wallet connected via ton_proof")
                else:
                    logger.warning("Fragment: ton_proof auth failed: %s", result)
        except Exception as e:
            logger.warning("Fragment: wallet connect error: %s", e)

    @property
    def ready(self) -> bool:
        return self._initialized and self._client is not None

    async def close(self) -> None:
        self._client = None
        self._initialized = False

    # ── Floor prices ──────────────────────────────────────────────────

    async def get_floor_price(self, collection_name: str) -> int:
        """Get floor price for a collection in nanoton. Returns 0 if not found.

        Validates that the first result actually belongs to this collection
        to avoid false prices when Fragment returns global results for
        unknown collection slugs.
        """
        if not self.ready:
            return 0

        slug = self._get_slug(collection_name)

        try:
            result = await self._client.search_gifts(
                collection=slug,
                sort="price_asc",
                filter="sale",
            )
            if result and result.items:
                first = result.items[0]
                # Validate: the result must belong to the requested collection.
                # If slug doesn't match a real Fragment collection, the API
                # returns ALL gifts globally (cheapest ~3 TON Vice Cream),
                # creating fake arbitrage signals.
                result_name = first.get("name", "")
                result_slug = _name_to_slug(result_name.rsplit("#", 1)[0].strip())
                if result_slug != slug:
                    return 0

                price_str = first.get("price", "0")
                price_ton = float(price_str)
                price_nano = int(price_ton * NANO)
                self._floor_cache[collection_name] = price_nano
                return price_nano
        except Exception as e:
            err_str = str(e).lower()
            if "401" in err_str or "403" in err_str or "unauthorized" in err_str:
                if not self._auth_alert_sent:
                    self._auth_alert_sent = True
                    logger.warning("Fragment: auth expired")
                    if self._on_auth_expired:
                        try:
                            await self._on_auth_expired()
                        except Exception:
                            pass
            else:
                logger.debug("Fragment floor fetch error for %s: %s", collection_name, e)

        return self._floor_cache.get(collection_name, 0)

    async def get_floor_prices_bulk(
        self, collections: list[str], max_per_cycle: int = 40
    ) -> dict[str, int]:
        """Fetch floor prices for multiple collections.

        Rate-limited to avoid hammering Fragment.
        Rotates through collections across cycles so all get scanned.
        Returns {collection_name: floor_price_nanoton}.
        """
        if not self.ready:
            return {}

        now = time.monotonic()
        # Don't re-fetch more often than every 120s
        if now - self._last_bulk_fetch < 120:
            return dict(self._floor_cache)

        # Rotate: start from where we left off last cycle
        offset = getattr(self, "_bulk_offset", 0)
        total = len(collections)
        if offset >= total:
            offset = 0
        batch = collections[offset : offset + max_per_cycle]
        if len(batch) < max_per_cycle and offset > 0:
            batch += collections[: max_per_cycle - len(batch)]
        self._bulk_offset = offset + max_per_cycle

        floors: dict[str, int] = {}
        fetched = 0

        for name in batch:
            try:
                price = await self.get_floor_price(name)
                if price > 0:
                    floors[name] = price
                fetched += 1
                # Rate limit: 0.3s between requests
                await asyncio.sleep(0.3)
            except Exception as e:
                logger.debug("Fragment bulk floor error for %s: %s", name, e)

        self._floor_cache.update(floors)
        self._last_bulk_fetch = now
        logger.info(
            "Fragment floors refreshed: %d/%d collections (offset=%d), %d cached total",
            fetched,
            len(collections),
            offset,
            len(self._floor_cache),
        )
        return dict(self._floor_cache)

    def get_cached_floor(self, collection_name: str) -> int:
        """Get cached floor price. Returns 0 if not cached."""
        val = self._floor_cache.get(collection_name, 0)
        if val > 0:
            return val

        # Try fuzzy matching (plural forms etc)
        name_lower = collection_name.lower()
        for cached_name, price in self._floor_cache.items():
            if cached_name.lower() == name_lower:
                return price
            # Try without 's' suffix
            if cached_name.lower().rstrip("s") == name_lower.rstrip("s"):
                return price

        return 0

    def get_all_floors(self) -> dict[str, int]:
        """Return all cached floor prices."""
        return dict(self._floor_cache)

    # ── Search ────────────────────────────────────────────────────────

    async def search_on_sale(self, collection_name: str, limit: int = 10) -> list[dict[str, Any]]:
        """Search on-sale gifts for a collection.

        Returns list of dicts with keys: slug, name, price (TON float), price_nano.
        """
        if not self.ready:
            return []

        slug = self._get_slug(collection_name)
        items: list[dict[str, Any]] = []

        try:
            result = await self._client.search_gifts(
                collection=slug,
                sort="price_asc",
                filter="sale",
            )
            if not result or not result.items:
                return []

            for g in result.items[:limit]:
                # Validate result belongs to requested collection
                result_name = g.get("name", "")
                result_slug = _name_to_slug(result_name.rsplit("#", 1)[0].strip())
                if result_slug != slug:
                    break  # All results are wrong collection

                price_str = g.get("price", "0")
                try:
                    price_ton = float(price_str)
                except (ValueError, TypeError):
                    continue
                items.append(
                    {
                        "slug": g.get("slug", ""),
                        "name": result_name,
                        "collection": collection_name,
                        "price_ton": price_ton,
                        "price_nano": int(price_ton * NANO),
                        "status": g.get("status", ""),
                        "date": g.get("date"),
                    }
                )
        except Exception as e:
            logger.debug("Fragment search error for %s: %s", collection_name, e)

        return items

    # ── Buy / Sell ─────────────────────────────────────────────────────

    async def buy_gift(self, gift_slug: str, price_ton: float = 0) -> dict[str, Any] | None:
        """Buy a gift on Fragment via blockchain transaction.

        Flow: getBidLink → sign tx → broadcast to TON blockchain.
        price_ton: listed "Buy Now" price (required by Fragment API).
        """
        if not self.ready:
            return None
        try:
            import json as _json

            from pyfragment.core.constants import DEVICE
            from pyfragment.domains.tonapi.account import get_account_info
            from pyfragment.domains.tonapi.transaction import process_transaction

            page_url = f"https://fragment.com/gift/{gift_slug}"
            account = await get_account_info(self._client)

            tx_data = await self._client.call(
                "getBidLink",
                {
                    "type": 5,
                    "username": gift_slug,
                    "bid": str(price_ton) if price_ton else "",
                    "account": _json.dumps(account),
                    "device": DEVICE,
                    "transaction": 1,
                },
                page_url=page_url,
            )
            if tx_data.get("error"):
                logger.error("Fragment buy %s API error: %s", gift_slug, tx_data["error"])
                return tx_data

            tx_hash = await process_transaction(self._client, tx_data)
            logger.info("Fragment buy %s: tx=%s", gift_slug, tx_hash)
            return {"ok": True, "tx_hash": tx_hash, "slug": gift_slug}
        except Exception as e:
            logger.error("Fragment buy error for %s: %s", gift_slug, e)
            return None

    async def put_on_sale(self, gift_slug: str, price_ton: float) -> dict[str, Any] | None:
        """List a gift for sale on Fragment via blockchain transaction.

        Flow: getStartAuctionLink → sign tx → broadcast.
        """
        if not self.ready:
            return None
        try:
            import json as _json

            from pyfragment.core.constants import DEVICE
            from pyfragment.domains.tonapi.account import get_account_info
            from pyfragment.domains.tonapi.transaction import process_transaction

            page_url = f"https://fragment.com/gift/{gift_slug}"
            account = await get_account_info(self._client)

            tx_data = await self._client.call(
                "getStartAuctionLink",
                {
                    "type": 5,
                    "username": gift_slug,
                    "min_amount": str(price_ton),
                    "max_amount": str(price_ton),
                    "account": _json.dumps(account),
                    "device": DEVICE,
                    "transaction": 1,
                },
                page_url=page_url,
            )
            if tx_data.get("error"):
                logger.error("Fragment list %s API error: %s", gift_slug, tx_data["error"])
                return tx_data

            tx_hash = await process_transaction(self._client, tx_data)
            logger.info("Fragment list %s at %.2f TON: tx=%s", gift_slug, price_ton, tx_hash)
            return {"ok": True, "tx_hash": tx_hash, "slug": gift_slug, "price": price_ton}
        except Exception as e:
            logger.error("Fragment list error for %s: %s", gift_slug, e)
            return None

    async def cancel_sale(self, gift_slug: str) -> dict[str, Any] | None:
        """Cancel a gift listing on Fragment via blockchain transaction."""
        if not self.ready:
            return None
        try:
            import json as _json

            from pyfragment.core.constants import DEVICE
            from pyfragment.domains.tonapi.account import get_account_info
            from pyfragment.domains.tonapi.transaction import process_transaction

            page_url = f"https://fragment.com/gift/{gift_slug}"
            account = await get_account_info(self._client)

            tx_data = await self._client.call(
                "getCancelAuctionLink",
                {
                    "type": 5,
                    "username": gift_slug,
                    "account": _json.dumps(account),
                    "device": DEVICE,
                    "transaction": 1,
                },
                page_url=page_url,
            )
            if tx_data.get("error"):
                logger.error("Fragment cancel %s API error: %s", gift_slug, tx_data["error"])
                return tx_data

            tx_hash = await process_transaction(self._client, tx_data)
            logger.info("Fragment cancel sale %s: tx=%s", gift_slug, tx_hash)
            return {"ok": True, "tx_hash": tx_hash, "slug": gift_slug}
        except Exception as e:
            logger.error("Fragment cancel sale error for %s: %s", gift_slug, e)
            return None

    async def send_to_telegram(
        self,
        gift_slug: str,
        recipient_username: str,
    ) -> dict[str, Any] | None:
        """Transfer an NFT from Fragment to a Telegram account.

        Flow: searchNftTransferRecipient → initNftTransferRequest
              → getNftTransferLink → sign tx → broadcast.
        """
        if not self.ready:
            return None
        try:
            import json as _json

            from pyfragment.core.constants import DEVICE
            from pyfragment.domains.tonapi.account import get_account_info
            from pyfragment.domains.tonapi.transaction import process_transaction

            page_url = f"https://fragment.com/gift/{gift_slug}"

            # Step 1: resolve recipient
            search = await self._client.call(
                "searchNftTransferRecipient",
                {"query": recipient_username},
                page_url=page_url,
            )
            found = search.get("found") or {}
            recipient_id = found.get("recipient")
            if not recipient_id:
                logger.error(
                    "Fragment transfer %s: recipient %s not found",
                    gift_slug,
                    recipient_username,
                )
                return None

            # Step 2: init transfer request
            init = await self._client.call(
                "initNftTransferRequest",
                {"recipient": recipient_id, "slug": gift_slug},
                page_url=page_url,
            )
            req_id = init.get("req_id")
            if not req_id:
                logger.error(
                    "Fragment transfer %s init error: %s",
                    gift_slug,
                    init.get("error", init),
                )
                return None

            # Step 3: get blockchain tx
            account = await get_account_info(self._client)
            tx_data = await self._client.call(
                "getNftTransferLink",
                {
                    "id": req_id,
                    "show_sender": 1,
                    "account": _json.dumps(account),
                    "device": DEVICE,
                    "transaction": 1,
                },
                page_url=page_url,
            )
            if tx_data.get("error"):
                logger.error(
                    "Fragment transfer %s tx error: %s",
                    gift_slug,
                    tx_data["error"],
                )
                return tx_data

            # Step 4: sign and broadcast
            tx_hash = await process_transaction(self._client, tx_data)
            logger.info(
                "Fragment transfer %s → %s: tx=%s",
                gift_slug,
                recipient_username,
                tx_hash,
            )
            return {
                "ok": True,
                "tx_hash": tx_hash,
                "slug": gift_slug,
                "recipient": recipient_username,
            }
        except Exception as e:
            logger.error("Fragment transfer error for %s: %s", gift_slug, e)
            return None

    async def get_wallet_balance(self) -> float:
        """Get wallet balance in TON."""
        if not self.ready:
            return 0.0
        try:
            wallet = await self._client.get_wallet()
            return wallet.ton_balance
        except Exception as e:
            logger.error("Fragment wallet balance error: %s", e)
            return 0.0

    # ── Internals ─────────────────────────────────────────────────────

    def _get_slug(self, collection_name: str) -> str:
        """Get Fragment slug for a collection name."""
        if collection_name in self._slug_cache:
            return self._slug_cache[collection_name]
        slug = _name_to_slug(collection_name)
        self._slug_cache[collection_name] = slug
        return slug
