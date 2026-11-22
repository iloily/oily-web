

# --- o!ily: change a map's ranked status from the website (added by server/oily-update.sh) ---
from fastapi import Query as _oily_Query

from app.api.v2.common.parameters import SessionCookie as _oily_SessionCookie
from app.constants.beatmap_statuses import RankedStatus as _oily_RankedStatus
from app.constants.privileges import Privileges as _oily_Privileges
from app.objects.beatmap import Beatmap as _oily_Beatmap
from app.repositories.legacy import get_legacy_repositories as _oily_repos
from app.services.web_sessions import WebSessionsService as _oily_WebSessionsService
import app.state as _oily_state

_OILY_STATUSES = {"rank": _oily_RankedStatus.Ranked, "love": _oily_RankedStatus.Loved, "unrank": _oily_RankedStatus.Pending}


@router.post("/maps/{map_id}/status")
async def oily_set_map_status(
    map_id: int,
    session_token: _oily_SessionCookie = None,
    *,
    action: str = _oily_Query(...),
    scope: str = _oily_Query("map"),
    web_sessions_service: Annotated[
        _oily_WebSessionsService,
        Depends(api_dependencies.get_web_sessions_service),
    ],
) -> Success[dict] | Failure:
    """Same as the in-game !map <rank/love/unrank> <map/set> command, for staff on the website."""
    if action not in _OILY_STATUSES or scope not in ("map", "set"):
        return responses.failure(message="Use action=rank|love|unrank and scope=map|set.", status_code=status.HTTP_400_BAD_REQUEST)
    user = await web_sessions_service.fetch_session_user(session_token) if session_token else None
    if user is None:
        return responses.failure(message="Authentication required.", status_code=status.HTTP_401_UNAUTHORIZED)
    if not user.priv & _oily_Privileges.NOMINATOR:
        return responses.failure(message="Only staff can change map status.", status_code=status.HTTP_403_FORBIDDEN)

    repos = _oily_repos()
    rec = await repos.maps.fetch_one(id=map_id)
    if rec is None:
        # not seen on this server yet: let bancho fetch it from the mirror first
        try:
            await _oily_Beatmap.from_bid(map_id)
        except Exception:
            pass
        rec = await repos.maps.fetch_one(id=map_id)
    if rec is None:
        return responses.failure(message="Map not found.", status_code=status.HTTP_404_NOT_FOUND)
    new_status = _OILY_STATUSES[action]

    async with _oily_state.services.database.transaction():
        if scope == "set":
            rows = await repos.maps.fetch_many(set_id=rec.set_id)
            ids = [row.id for row in rows]
            for i in ids:
                await repos.maps.partial_update(i, status=new_status, frozen=True)
        else:
            ids = [rec.id]
            await repos.maps.partial_update(rec.id, status=new_status, frozen=True)

        # keep bancho's in-memory map cache in step with the database
        for cached in list(_oily_state.cache.beatmap.values()):
            if cached.id in ids:
                cached.status = new_status
                cached.frozen = True
        await repos.map_requests.mark_batch_as_inactive(map_ids=ids)

    return responses.success({"map_ids": ids, "status": int(new_status)})
