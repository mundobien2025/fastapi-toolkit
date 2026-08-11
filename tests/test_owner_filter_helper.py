"""`BaseService.owner_filter()` (Beanie) — helper que decide la clave Mongo
correcta para un filtro de ownership sobre un campo `Link`, según si la
query va a correr agregada (`list()`, con `fetch_links=True` → Beanie la
reescribe a `$lookup`, clave post-lookup `"<field>._id"`) o plana
(`retrieve`/`update`/`delete` vía `get_by_id`, que SIEMPRE escopea con
`fetch_links=False` → DBRef intacto, clave `"<field>.$id"`).

Caso real que motivó el helper (pulbot-backend, `ToolService`, 2026-08-11):
un service con `Link[User]` usaba la clave equivocada en `retrieve`/
`update`/`delete` (la misma que `list()`) — no tiraba error, devolvía 0
resultados en silencio → 404 en el delete de un recurso propio. Verificado
contra Mongo real (pulbot-backend, docker) tras el fix: 72/72 tests de
`app/tests/e2e/tool*` en verde.

Nota: este test es de LÓGICA PURA (sin DB) a propósito — `mongomock_motor`
(el mock que usa el resto de esta suite) no soporta queries dot-notation
sobre `DBRef` (`{"owner.$id": ...}` da 0 matches incluso contra el mismo
documento que lo tiene), así que un test contra mongomock de la mitad
`.$id` de este helper daría un falso negativo. La corrección de esa mitad
ya está verificada contra Mongo real en pulbot-backend; acá solo
caracterizamos que `owner_filter()` arma la clave correcta por `action`.
"""

from types import SimpleNamespace

from fastapi_basekit.aio.beanie.service.base import BaseService


class _FakeRepo:
    pass


def _service(action: str) -> BaseService:
    svc = BaseService(repository=_FakeRepo(), request=None)
    svc.action = action
    return svc


def test_owner_filter_uses_post_lookup_key_for_list():
    svc = _service("list")
    assert svc.owner_filter("user", "u1") == {"user._id": "u1"}


def test_owner_filter_uses_dbref_key_for_retrieve():
    svc = _service("retrieve")
    assert svc.owner_filter("user", "u1") == {"user.$id": "u1"}


def test_owner_filter_uses_dbref_key_for_update():
    svc = _service("update")
    assert svc.owner_filter("user", "u1") == {"user.$id": "u1"}


def test_owner_filter_uses_dbref_key_for_delete():
    svc = _service("delete")
    assert svc.owner_filter("user", "u1") == {"user.$id": "u1"}


def test_owner_filter_defaults_to_dbref_key_for_custom_actions():
    """Cualquier action que no sea "list" (ej. un endpoint custom que llama
    `get_filters()` a mano) asume el camino no-agregado — es el lado
    seguro: si algún día un action custom SÍ corre agregado, hay que optar
    explícitamente pasando `"list"` o extendiendo el criterio, no al revés."""
    svc = _service("approve")
    assert svc.owner_filter("customer", "c1") == {"customer.$id": "c1"}
