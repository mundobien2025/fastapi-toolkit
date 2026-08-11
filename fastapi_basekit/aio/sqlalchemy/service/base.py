from typing import Any, Dict, Generic, List, Optional, Tuple

from fastapi import Request
from pydantic import BaseModel
from sqlalchemy import select
from ..repository.base import BaseRepository, ModelT
from ....exceptions.api_exceptions import (
    APIException,
    NotFoundException,
    DatabaseIntegrityException,
)


class BaseService(Generic[ModelT]):
    """Servicio base para SQLAlchemy AsyncSession, parametrizado por el modelo.

    Declara el modelo vía el genérico para tipar el CRUD::

        class UserService(BaseService[User]):
            repository: UserRepository

    Así ``retrieve``/``create``/``update`` devuelven ``User`` y ``list`` un
    ``tuple[list[User], int]``.

    Regla del proyecto: los servicios NO deben llamar `session.flush()`,
    `session.commit()` ni `session.refresh()`. El flush vive en
    `BaseRepository.create / update`; el commit/rollback único por
    request lo gestiona el lifecycle creado con
    `fastapi_basekit.aio.sqlalchemy.make_session_lifecycle`.
    """

    repository: BaseRepository[ModelT]
    search_fields: List[str] = []
    duplicate_check_fields: List[str] = []
    order_by: Optional[str] = None
    action: str | None = None
    kwargs_query: Dict[str, Any] = {}

    # --- Política de borrado (ver `delete`) ---
    #   "hard"           -> elimina físicamente (default, comportamiento histórico)
    #   "soft"           -> marca deleted_at (requiere model con soft_delete())
    #   "soft_mangle"    -> soft + renombra `mangle_fields` (`<valor>__del_<id>`)
    #                       para liberar un valor único y poder recrear el registro
    #   "hard_if_unused" -> elimina físicamente si no está referenciado, si no -> 409
    delete_mode: str = "hard"
    mangle_fields: List[str] = []
    # [(Model, "fk_attr"), ...] revisados en "hard_if_unused".
    delete_references: List[Any] = []

    def __init__(
        self,
        repository: BaseRepository,
        request: Optional[Request] = None,
        **kwargs,
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
        self.mangle_fields = list(self.mangle_fields)
        self.delete_references = list(self.delete_references)

        # Vincular el servicio al repositorio principal
        if self.repository:
            self.repository.service = self

        # Procesar kwargs adicionales para vincular otros repositorios
        for name, value in kwargs.items():
            if isinstance(value, BaseRepository):
                value.service = self
            setattr(self, name, value)
        endpoint_func = (
            self.request.scope.get("endpoint") if self.request else None
        )
        self.action = endpoint_func.__name__ if endpoint_func else None

        # Parámetros compartidos para consultas (especialmente list)
        self.params: Dict[str, Any] = {
            "search": None,
            "page": 1,
            "count": 25,
            "filters": {},
            "use_or": False,
            "joins": None,
            "order_by": self.order_by,
            "search_fields": self.search_fields,
            "meta": {},
        }

    def get_filters(
        self, filters: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Sobrescribe para validar/transformar filtros entrantes
        antes de consultar."""
        return filters or {}

    def get_kwargs_query(self) -> Dict[str, Any]:
        """Sobrescribe para retornar kwargs de consulta para el repositorio.

        Ejemplo de uso en un servicio:

            def get_kwargs_query(self):
                if self.action in ["retrieve", "list"]:
                    return {"joins": ["role"]}
                return super().get_kwargs_query()

        """
        return self.kwargs_query or {}

    async def retrieve(
        self, id: str, joins: Optional[List[str]] = None
    ) -> ModelT:
        # Permite que el servicio defina joins u otros kwargs por acción
        kwargs = self.get_kwargs_query()
        if joins is None:
            joins = kwargs.get("joins")

        filters = self.get_filters()
        obj = await self.repository.get_with_joins(id, joins=joins, filters=filters)
        if not obj:
            obj = await self.repository.get(id, filters=filters)
        if not obj:
            raise NotFoundException(f"id={id} no encontrado")
        return obj

    async def list(
        self,
        search: Optional[str] = None,
        page: Optional[int] = None,
        count: Optional[int] = None,
        filters: Optional[Dict[str, Any]] = None,
        use_or: Optional[bool] = None,
        joins: Optional[List[str]] = None,
        order_by: Optional[Any] = None,
    ) -> Tuple[List[ModelT], int]:
        # Actualiza self.params con los argumentos
        # proporcionados (si no son None)
        if search is not None:
            self.params["search"] = search
        if page is not None:
            self.params["page"] = page
        if count is not None:
            self.params["count"] = count
        if filters is not None:
            self.params["filters"] = filters
        if use_or is not None:
            self.params["use_or"] = use_or
        if joins is not None:
            self.params["joins"] = joins
        if order_by is not None:
            self.params["order_by"] = order_by

        # Aplica filtros y kwargs de consulta definidos por el servicio
        applied_filters = self.get_filters(self.params["filters"])
        kwargs = self.get_kwargs_query()

        # Prioridad de joins: argumento explícito >
        # kwargs del servicio (por acción)
        final_joins = self.params["joins"]
        if final_joins is None:
            final_joins = kwargs.get("joins")

        # Prioridad de order_by: argumento explícito >
        # kwargs del servicio > default del servicio
        final_order_by = self.params["order_by"]
        if order_by is None:
            final_order_by = kwargs.get("order_by", self.params["order_by"])

        items, total = await self.repository.list_paginated(
            page=self.params["page"],
            count=self.params["count"],
            filters=applied_filters,
            use_or=self.params["use_or"],
            joins=final_joins,
            order_by=final_order_by,
            search=self.params["search"],
            search_fields=self.params["search_fields"],
        )
        items = await self.post_process_list(items)
        return items, total

    async def post_process_list(self, items: List[ModelT]) -> List[ModelT]:
        """Hook: transforma/enriquece los items DE UNA PÁGINA ya paginada.

        El método para "hacer algo custom con los resultados" SIN reescribir la
        paginación ni overridear `list()`. Corre después de `list_paginated`,
        sobre los items de la página actual. Default: sin cambios. Ejemplo::

            async def post_process_list(self, items):
                for r in items:
                    r.display_url = build_url(r.slug)
                return items

        NO cambies `total` ni filtres items acá (usa `get_filters`/
        `build_list_queryset` para filtrar, si no el total queda mal).
        """
        return items

    async def create(
        self,
        payload: BaseModel | Dict[str, Any],
        check_fields: Optional[List[str]] = None,
    ) -> ModelT:
        data = (
            payload.model_dump() if isinstance(payload, BaseModel) else payload
        )
        fields = (
            check_fields
            if check_fields is not None
            else self.duplicate_check_fields
        )
        if fields:
            filters = {f: data[f] for f in fields if f in data}
            if filters:
                existing = await self.repository.get_by_filters(filters)
                if existing:
                    raise DatabaseIntegrityException(
                        message="Registro ya existe", data=filters
                    )
        created = await self.repository.create(data)
        return created

    async def update(self, id: str, data: BaseModel | Dict[str, Any]) -> ModelT:
        # Scoping de seguridad ANTES de tocar nada — `repository.update`
        # fetchea por PK cruda internamente (sin `get_filters()`), así que
        # sin este check un id de otro tenant se podía editar directo.
        scoped = await self.repository.get(id, filters=self.get_filters())
        if not scoped:
            raise NotFoundException(f"id={id} no encontrado")
        update_data = (
            data.model_dump(exclude_unset=True)
            if isinstance(data, BaseModel)
            else data
        )
        updated = await self.repository.update(id, update_data)
        return updated

    async def _referenced_label(self, obj: Any) -> Optional[str]:
        """Nombre de un modelo que aún referencia a `obj` (bloquea hard delete), o None."""
        session = self.repository.session
        for model, fk in self.delete_references:
            query = select(model.id).where(getattr(model, fk) == obj.id)
            if hasattr(model, "deleted_at"):
                query = query.where(model.deleted_at.is_(None))
            if (await session.execute(query.limit(1))).first():
                return model.__name__
        return None

    async def apply_delete(self, obj: Any) -> bool:
        """Aplica la política de borrado (`delete_mode`) sobre una entidad ya cargada.

        Útil cuando una subclase necesita resolver/scopear el objeto antes de borrar
        (p. ej. tenant + region scope): hace `obj = self._get_scoped(id)` y luego
        `await self.apply_delete(obj)`.
        """
        mode = self.delete_mode
        if mode in ("soft", "soft_mangle"):
            if not hasattr(obj, "soft_delete"):  # modelo sin soft delete -> físico
                await self.repository.hard_delete(obj)
                return True
            obj.soft_delete()
            if mode == "soft_mangle":
                for field in self.mangle_fields:
                    current = getattr(obj, field, None)
                    if current is not None:
                        setattr(obj, field, f"{current}__del_{obj.id}")
            await self.repository.save(obj)
        elif mode == "hard_if_unused":
            if await self._referenced_label(obj) is not None:
                raise APIException(
                    message="No se puede eliminar: el registro está en uso.",
                    status_code="IN_USE", status=409,
                )
            await self.repository.hard_delete(obj)
        else:  # "hard"
            await self.repository.hard_delete(obj)
        return True

    async def delete(self, id: str) -> bool:
        obj = await self.repository.get(id, filters=self.get_filters())
        if not obj:
            raise NotFoundException(message=f"id={id} no encontrado")
        return await self.apply_delete(obj)
