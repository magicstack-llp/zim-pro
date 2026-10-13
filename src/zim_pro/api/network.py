"""The VPN and the captcha solver: what they report, and asking the VPN for a new address."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..context import Context, get_context, require_admin
from ..network import CONTROL, NetworkError

router = APIRouter(prefix="/api/network")


@router.get("")
async def network_status(
    fresh: bool = False, ctx: Context = Depends(get_context)
) -> dict[str, Any]:
    return await ctx.network.status(fresh)


@router.post("/change-ip", dependencies=[Depends(require_admin)])
async def change_ip(ctx: Context = Depends(get_context)) -> dict[str, Any]:
    """Reconnect the VPN now, whatever was last done, and say what it came back as."""
    try:
        changed = await ctx.network.change_ip(forced=True)
    except NetworkError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"changed": changed, **await ctx.network.status(fresh=True)}


class TestBody(BaseModel):
    """What is in the form right now, saved or not."""

    kind: str = "vpn"
    build_proxy: str = Field(default="", max_length=2000)
    vpn_control_url: str = Field(default="", max_length=2000)
    vpn_control_key: str = Field(default="", max_length=2000)
    vpn_control_user: str = Field(default="", max_length=2000)
    vpn_control_password: str = Field(default="", max_length=2000)
    captcha_solver_url: str = Field(default="", max_length=2000)


SECRET_MASK = "••••••••"  # what the form shows in place of a saved secret


@router.post("/test", dependencies=[Depends(require_admin)])
async def test_connection(body: TestBody, ctx: Context = Depends(get_context)) -> dict[str, Any]:
    """Try the VPN or the solver with the addresses as typed, without saving them."""
    typed = {key: getattr(body, key).strip() for key in (*CONTROL, "build_proxy")}
    for key, value in typed.items():
        if value == SECRET_MASK:  # unchanged in the form: the saved one is meant
            typed[key] = str(ctx.db.get_setting(key))
    for key in ("build_proxy", "vpn_control_url"):
        if typed[key] and not typed[key].startswith(("http://", "https://")):
            raise HTTPException(422, "Addresses must start with http:// or https://")
    if body.kind == "solver":
        address = body.captcha_solver_url.strip()
        if not address.startswith(("http://", "https://")):
            raise HTTPException(422, "Type the solver's address, starting with http://")
        return {"solver": await ctx.network._look_at_solver(address)}
    if not typed["build_proxy"] and not typed["vpn_control_url"]:
        raise HTTPException(422, "Type the proxy address or the control server address first.")
    return await ctx.network.try_vpn(typed)
