from typing import Any, Dict, Generic, List, Optional, Tuple, Union

from beanie import Document
from beanie.odm.queries.find import FindMany
from fastapi import Request
from pydantic import BaseModel


from ...beanie.repository.base import BaseRepository, ModelT
from ....exceptions.api_exceptions import (
    NotFoundException,
    DatabaseIntegrityException,
)


class BaseService(Generic[ModelT]):
    """Servicio base específico para Beanie ODM (async), parametrizado.

    Declara el modelo vía el genérico para tipar el CRUD::

        class UserService(BaseService[User]):
            repository: UserRepository

    Así ``retrieve``/``create``/``update`` devuelven ``User``.
    """

    repository: BaseRepository[ModelT]
    search_fields: List[str] = []
    duplicate_check_fields: List[str] = []
    kwargs_query: Dict[str, Union[str, int]] = {}
    action: str = ""
    order_by: Optional[List[tuple]] = None
    use_aggregation: bool = False
    aggregation_validate: bool = True

    def __init__(
        self, repository: BaseRepository, request: Optional[Request] = None
    ):
        self.repository = repository
        self.request = request
        # Copia por instancia de los defaults mutables (heredados como
        # atributos de CLASE, compartidos por todo el proceso). Sin esto, una
        # mutación en runtime (`self.search_fields.append(...)`) se filtraría a
        # la clase y contaminaría otras requests. Respeta el override del
        # subclass: `list(self.search_fields)` lee su atributo de clase.
        self.search_fields = list(self.search_fields)
        self.duplicate_check_fields = list(self.duplicate_check_fields)
        self.kwargs_query = dict(self.kwargs_query)
        endpoint_func = (
            self.request.scope.get("endpoint") if self.request else None
        )
        self.action = endpoint_func.__name__ if endpoint_func else None

    async def _check_duplicate(self, data: Dict[str, Any], fields: List[str]):
        filters = {f: data[f] for f in fields if f in data}
        if not filters:
            return

        existing = await self.repository.get_by_fields(filters)
        if existing:
            raise DatabaseIntegrityException(
                message="Registro ya existe",
                data=filters,
            )

    def get_kwargs_query(self) -> Dict[str, Any]:
        return self.kwargs_query

    def get_order(self) -> Optional[List[tuple]]:
        """Override this method to define custom ordering.
        
        Returns:
            List of tuples with (field_name, direction) where direction is 1 for ascending or -1 for descending.
            Example: [("created_at", -1)] for newest first
        """
        return self.order_by

    def get_filters(
        self,
        filters: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        filters = filters or {}
        return filters

    def owner_filter(self, field: str, value: Any) -> Dict[str, Any]:
        """Filtro de scoping por un campo `Link` (ej. ownership por
        `user`), correcto tanto en `list()` como en `retrieve`/`update`/
        `delete` — sin que el service tenga que conocer por qué la clave
        Mongo cambia entre esos dos caminos.

        Por qué existen dos claves para "lo mismo": un campo `Link[User]`
        se guarda como DBRef (`{"$ref": "users", "$id": ObjectId(...)}`).
        - `retrieve`/`update`/`delete` van por `get_by_id`, que SIEMPRE
          resuelve el scope con `fetch_links=False` (`find_one` plano sobre
          el documento crudo) — ahí el DBRef está intacto, la clave correcta
          es `"<field>.$id"`.
        - `list()` (y cualquier action cuyo `get_kwargs_query()` pida
          `fetch_links=True`) corre `Model.find(..., fetch_links=True)`,
          que Beanie reescribe a una aggregation con `$lookup` — ahí el
          `$lookup` REEMPLAZA el DBRef por el subdocumento resuelto, así
          que la clave correcta pasa a ser `"<field>._id"`.
        Usar la clave equivocada no tira error — devuelve 0 resultados en
        silencio (bug real ya encontrado usando esto a mano en
        `ToolService` de pulbot-backend, 2026-08-11). Este helper decide
        por vos: usalo en vez de escribir `"<field>.$id"`/`"<field>._id"`
        directo en tu `get_filters()`.

        ⚠️ Asume que tu action de lectura-por-id (`retrieve`/`update`/
        `delete`) no fuerza `fetch_links=True` en el PROPIO chequeo de
        scope — así es como quedó `get_by_id` del repo (siempre
        `fetch_links=False` para el scope-check, independientemente del
        `fetch_links` que pidas para el fetch final). Si tu repo está
        parcheado para saltarse eso, este helper no aplica."""
        suffix = "._id" if self.action == "list" else ".$id"
        return {f"{field}{suffix}": value}

    async def retrieve(self, id: str) -> ModelT:
        kwargs = self.get_kwargs_query()
        obj = await self.repository.get_by_id(
            id, filters=self.get_filters(), **kwargs
        )
        if not obj:
            raise NotFoundException(f"id={id} no encontrado")
        return obj

    def build_list_queryset(
        self,
        search: Optional[str] = None,
        search_fields: Optional[List[str]] = None,
        filters: Optional[Dict[str, Any]] = None,
        order_by: Optional[List[tuple]] = None,
        **kwargs,
    ) -> FindMany[Document]:
        """Service-level hook over `repository.build_list_queryset`.

        Override here to compose query options across repositories or to
        decorate the FindMany before pagination.
        """
        return self.repository.build_list_queryset(
            search=search,
            search_fields=search_fields or self.search_fields,
            filters=filters,
            order_by=order_by,
            **kwargs,
        )

    def build_list_pipeline(
        self,
        search: Optional[str] = None,
        search_fields: Optional[List[str]] = None,
        filters: Optional[Dict[str, Any]] = None,
        order_by: Optional[str] = None,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        """Service-level hook over `repository.build_list_pipeline`.

        Override here to add `$lookup`, `$project`, `$group`, etc. for
        cross-collection joins (subquery-like). Set `use_aggregation = True`
        on the service to force the pipeline path even without nested order.
        Set `aggregation_validate = False` if the projection produces a
        non-model shape (joined columns / flattened rows).
        """
        return self.repository.build_list_pipeline(
            search=search,
            search_fields=search_fields or self.search_fields,
            filters=filters,
            order_by=order_by,
            **kwargs,
        )

    async def list(
        self,
        search: Optional[str] = None,
        page: int = 1,
        count: int = 25,
        filters: Optional[Dict[str, Any]] = None,
        order_by: Optional[str] = None,  # Dynamic ordering (e.g., "-created_at" or "tool__name")
    ) -> Tuple[List[ModelT], int]:
        kwargs = self.get_kwargs_query()
        applied_filters = self.get_filters(filters)

        # Resolve ordering
        if order_by:
            order_str = order_by
        else:
            default_order = self.get_order()
            if default_order:
                field, direction = default_order[0]
                order_str = f"{'-' if direction == -1 else ''}{field}"
            else:
                order_str = None

        nested_order = bool(order_str and ("__" in order_str or "." in order_str))
        use_pipeline = self.use_aggregation or nested_order

        if use_pipeline:
            pipeline = self.build_list_pipeline(
                search=search,
                search_fields=self.search_fields,
                filters=applied_filters,
                order_by=order_str,
                **kwargs,
            )
            items, total = await self.repository.paginate_pipeline(
                pipeline,
                page=page,
                count=count,
                validate=self.aggregation_validate,
            )
        else:
            # FindMany path
            order_list = None
            if order_str:
                direction = -1 if order_str.startswith("-") else 1
                field = order_str.lstrip("-")
                order_list = [(field, direction)]

            query = self.build_list_queryset(
                search=search,
                search_fields=self.search_fields,
                filters=applied_filters,
                order_by=order_list,
                **kwargs,
            )
            items, total = await self.repository.paginate(
                query, page, count, order_by=order_list
            )

        items = await self.post_process_list(items)
        return items, total

    async def post_process_list(self, items: List[ModelT]) -> List[ModelT]:
        """Hook: transforma/enriquece los items DE UNA PÁGINA ya paginada.

        Este es el método para "hacer algo custom con los resultados" SIN
        reescribir la paginación ni overridear `list()`. Corre después de
        `paginate`/`paginate_pipeline`, sobre los items de la página actual
        (no toda la colección), así que es seguro para un enrich con un `await`
        extra por página (agrega un contador, resuelve un campo derivado, etc.).

        Default: devuelve los items sin cambios. Ejemplo::

            async def post_process_list(self, items):
                ids = [c.id for c in items]
                counts = await self.conv_repo.count_by_customer_ids(ids)
                for c in items:
                    c.conversation_count = counts.get(c.id, 0)
                return items

        NO cambies el `total` acá (es el de la query completa). NO filtres items
        (eso va en `get_filters`/`build_list_*`, o el total quedaría mal).
        """
        return items

    async def create(
        self, payload: BaseModel, check_fields: Optional[List[str]] = None
    ) -> ModelT:
        data = (
            payload.model_dump() if not isinstance(payload, dict) else payload
        )
        fields = (
            check_fields
            if check_fields is not None
            else self.duplicate_check_fields
        )
        if fields:
            await self._check_duplicate(data, fields)
        created = await self.repository.create(data)
        kwargs = self.get_kwargs_query()
        return (
            await self.repository.get_by_id(created.id, **kwargs)
            if kwargs
            else created
        )

    async def update(self, id: str, data: BaseModel) -> ModelT:
        kwargs = self.get_kwargs_query()
        obj = await self.repository.get_by_id(
            id, filters=self.get_filters(), **kwargs
        )
        if not obj:
            raise NotFoundException(f"id={id} no encontrado")
        if isinstance(data, BaseModel):
            data = data.model_dump(exclude_unset=True)
        updated = await self.repository.update(obj, data)
        return updated

    async def delete(self, id: str) -> str:
        obj = await self.repository.get_by_id(id, filters=self.get_filters())
        if not obj:
            raise NotFoundException(f"id={id} no encontrado")
        await self.repository.delete(obj)
        return "deleted"
