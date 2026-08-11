"""Regresión del BaseService de Beanie — la capa que pulbot usa más.

Cubre: retrieve (found/404), list (filtros/search/order/paginación), create
(dict/schema + duplicate check), update (resuelve id→Document internamente,
`exclude_unset`), delete (404), y el seam `get_filters` de scoping por tenant
(crítico de seguridad: separa QUÉ ve cada usuario).
"""

from typing import Optional

import mongomock_motor
import pytest
from beanie import Document, init_beanie
from pydantic import BaseModel

from fastapi_basekit.aio.beanie.repository.base import BaseRepository
from fastapi_basekit.aio.beanie.service.base import BaseService
from fastapi_basekit.exceptions.api_exceptions import (
    NotFoundException,
    DatabaseIntegrityException,
)


class Item(Document):
    name: str
    tenant: str
    qty: int = 0
    active: bool = True

    class Settings:
        name = "items"


class ItemRepo(BaseRepository):
    model = Item


class ItemService(BaseService):
    search_fields = ["name"]
    duplicate_check_fields = ["name"]


class ScopedItemService(BaseService):
    """Service con scoping por tenant fijo (simula get_filters de pulbot)."""

    search_fields = ["name"]

    def __init__(self, repository, tenant):
        super().__init__(repository=repository, request=None)
        self._tenant = tenant

    def get_filters(self, filters=None):
        filters = super().get_filters(filters)
        filters["tenant"] = self._tenant
        return filters


class ItemCreate(BaseModel):
    name: str
    tenant: str
    qty: int = 0


class ItemUpdate(BaseModel):
    name: Optional[str] = None
    qty: Optional[int] = None
    active: Optional[bool] = None


@pytest.fixture
async def db():
    client = mongomock_motor.AsyncMongoMockClient()
    await init_beanie(database=client.test_db, document_models=[Item])
    yield
    client.close()


@pytest.fixture
async def seeded(db):
    repo = ItemRepo()
    rows = [
        {"name": "Alpha", "tenant": "t1", "qty": 5, "active": True},
        {"name": "Beta", "tenant": "t1", "qty": 0, "active": False},
        {"name": "Gamma", "tenant": "t2", "qty": 9, "active": True},
    ]
    created = [await repo.create(r) for r in rows]
    return created


# ---------------------------------------------------------------------------
# retrieve
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retrieve_found(seeded):
    svc = ItemService(repository=ItemRepo())
    got = await svc.retrieve(str(seeded[0].id))
    assert got.name == "Alpha"


@pytest.mark.asyncio
async def test_retrieve_not_found(db):
    from bson import ObjectId
    svc = ItemService(repository=ItemRepo())
    with pytest.raises(NotFoundException):
        await svc.retrieve(str(ObjectId()))


# ---------------------------------------------------------------------------
# create + duplicate check
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_from_schema(db):
    svc = ItemService(repository=ItemRepo())
    obj = await svc.create(ItemCreate(name="New", tenant="t1", qty=3))
    assert obj.id is not None
    assert obj.qty == 3


@pytest.mark.asyncio
async def test_create_from_dict(db):
    svc = ItemService(repository=ItemRepo())
    obj = await svc.create({"name": "Dict", "tenant": "t1"})
    assert obj.name == "Dict"


@pytest.mark.asyncio
async def test_create_duplicate_raises(seeded):
    svc = ItemService(repository=ItemRepo())
    with pytest.raises(DatabaseIntegrityException):
        await svc.create(ItemCreate(name="Alpha", tenant="t1"))


@pytest.mark.asyncio
async def test_create_duplicate_check_override(seeded):
    """check_fields=[] desactiva la verificación de duplicados."""
    svc = ItemService(repository=ItemRepo())
    obj = await svc.create(ItemCreate(name="Alpha", tenant="t9"), check_fields=[])
    assert obj.id is not None


# ---------------------------------------------------------------------------
# update: resuelve id→Document, exclude_unset
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_update_by_id(seeded):
    svc = ItemService(repository=ItemRepo())
    updated = await svc.update(str(seeded[0].id), ItemUpdate(qty=100))
    assert updated.qty == 100
    assert updated.name == "Alpha"  # no tocado (exclude_unset)


@pytest.mark.asyncio
async def test_update_not_found(db):
    from bson import ObjectId
    svc = ItemService(repository=ItemRepo())
    with pytest.raises(NotFoundException):
        await svc.update(str(ObjectId()), ItemUpdate(qty=1))


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete(seeded):
    svc = ItemService(repository=ItemRepo())
    res = await svc.delete(str(seeded[2].id))
    assert res == "deleted"
    with pytest.raises(NotFoundException):
        await svc.retrieve(str(seeded[2].id))


@pytest.mark.asyncio
async def test_delete_not_found(db):
    from bson import ObjectId
    svc = ItemService(repository=ItemRepo())
    with pytest.raises(NotFoundException):
        await svc.delete(str(ObjectId()))


# ---------------------------------------------------------------------------
# list: filtros/search/paginación
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_all(seeded):
    svc = ItemService(repository=ItemRepo())
    items, total = await svc.list(page=1, count=50)
    assert total == 3


@pytest.mark.asyncio
async def test_list_filter_scalar(seeded):
    svc = ItemService(repository=ItemRepo())
    items, total = await svc.list(filters={"active": True}, count=50)
    assert {i.name for i in items} == {"Alpha", "Gamma"}


@pytest.mark.asyncio
async def test_list_search(seeded):
    svc = ItemService(repository=ItemRepo())
    items, total = await svc.list(search="Alph", count=50)
    assert {i.name for i in items} == {"Alpha"}


@pytest.mark.parametrize("page,count,expected", [
    (1, 1, 1), (1, 2, 2), (2, 2, 1), (1, 10, 3),
])
@pytest.mark.asyncio
async def test_list_pagination(seeded, page, count, expected):
    svc = ItemService(repository=ItemRepo())
    items, total = await svc.list(page=page, count=count)
    assert len(items) == expected
    assert total == 3


# ---------------------------------------------------------------------------
# get_filters scoping (seguridad: separa por tenant)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scoped_service_only_sees_its_tenant(seeded):
    svc_t1 = ScopedItemService(repository=ItemRepo(), tenant="t1")
    items, total = await svc_t1.list(count=50)
    assert total == 2
    assert all(i.tenant == "t1" for i in items)


@pytest.mark.asyncio
async def test_scoped_service_other_tenant(seeded):
    svc_t2 = ScopedItemService(repository=ItemRepo(), tenant="t2")
    items, total = await svc_t2.list(count=50)
    assert {i.name for i in items} == {"Gamma"}


@pytest.mark.asyncio
async def test_scoped_filter_merges_with_user_filter(seeded):
    """El filtro de scoping se combina con el filtro del usuario."""
    svc_t1 = ScopedItemService(repository=ItemRepo(), tenant="t1")
    items, total = await svc_t1.list(filters={"active": True}, count=50)
    assert {i.name for i in items} == {"Alpha"}  # t1 + active


# ---------------------------------------------------------------------------
# get_filters scoping en retrieve/update/delete (IDOR — CLAUDE.md prometía
# esto ["Si necesitas un objeto por id con scope, usa `service.retrieve(id)`"]
# pero `retrieve`/`update`/`delete` llamaban `repository.get_by_id(id)`
# directo, bypasseando `get_filters()` igual que un `get_by_id` crudo. Un
# `ScopedItemService` (tenant t2) podía leer/editar/borrar un Item de t1
# solo con adivinar/enumerar su id. Fix: `retrieve`/`update`/`delete` ahora
# pasan `filters=self.get_filters()` a `repository.get_by_id`.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retrieve_respects_scope_own_tenant_ok(seeded):
    svc_t1 = ScopedItemService(repository=ItemRepo(), tenant="t1")
    got = await svc_t1.retrieve(str(seeded[0].id))  # Alpha, tenant t1
    assert got.name == "Alpha"


@pytest.mark.asyncio
async def test_retrieve_cross_tenant_id_is_not_found(seeded):
    """El id de Gamma (t2) existe de verdad — pedido desde un service
    scopeado a t1 debe comportarse como si no existiera (404), no leakear
    el registro de otro tenant."""
    svc_t1 = ScopedItemService(repository=ItemRepo(), tenant="t1")
    with pytest.raises(NotFoundException):
        await svc_t1.retrieve(str(seeded[2].id))  # Gamma, tenant t2

    # El mismo id SÍ resuelve para el tenant dueño.
    svc_t2 = ScopedItemService(repository=ItemRepo(), tenant="t2")
    got = await svc_t2.retrieve(str(seeded[2].id))
    assert got.name == "Gamma"


@pytest.mark.asyncio
async def test_update_cross_tenant_id_is_not_found_and_not_modified(seeded):
    svc_t1 = ScopedItemService(repository=ItemRepo(), tenant="t1")
    with pytest.raises(NotFoundException):
        await svc_t1.update(str(seeded[2].id), ItemUpdate(qty=999))

    # No se modificó — lo confirma el dueño real (t2).
    svc_t2 = ScopedItemService(repository=ItemRepo(), tenant="t2")
    unchanged = await svc_t2.retrieve(str(seeded[2].id))
    assert unchanged.qty == 9


@pytest.mark.asyncio
async def test_delete_cross_tenant_id_is_not_found_and_not_deleted(seeded):
    svc_t1 = ScopedItemService(repository=ItemRepo(), tenant="t1")
    with pytest.raises(NotFoundException):
        await svc_t1.delete(str(seeded[2].id))

    # Sigue existiendo — lo confirma el dueño real (t2).
    svc_t2 = ScopedItemService(repository=ItemRepo(), tenant="t2")
    still_there = await svc_t2.retrieve(str(seeded[2].id))
    assert still_there.name == "Gamma"


@pytest.mark.asyncio
async def test_unscoped_service_retrieve_update_delete_unaffected(seeded):
    """Retrocompatibilidad: un service SIN override de `get_filters` (el
    caso común, `get_filters` devuelve `{}`) sigue viendo/editando/borrando
    cualquier id — cero cambio de comportamiento para el 99% de los
    services existentes que no scopean por owner."""
    svc = ItemService(repository=ItemRepo())
    got = await svc.retrieve(str(seeded[2].id))  # Gamma, sin scoping
    assert got.name == "Gamma"

    updated = await svc.update(str(seeded[2].id), ItemUpdate(qty=42))
    assert updated.qty == 42

    res = await svc.delete(str(seeded[2].id))
    assert res == "deleted"
