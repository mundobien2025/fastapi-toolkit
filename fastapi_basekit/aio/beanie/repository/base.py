import logging
from typing import Any, Dict, Generic, List, Optional, Type, TypeVar, Union
from typing import get_args, get_origin
import re

from bson import ObjectId
from pydantic import BaseModel
from beanie import Document, Link
from beanie.odm.queries.find import FindMany
from beanie.operators import Or, RegEx

logger = logging.getLogger(__name__)

#: Tipo del Document Beanie que gestiona el repositorio. Parametrízalo al
#: declarar la subclase — ``class UserRepository(BaseRepository[User])`` — para
#: que ``get_by_id``/``create``/``update``/``list_all`` devuelvan el Document
#: tipado (autocompletado + mypy para consumidores e IAs).
ModelT = TypeVar("ModelT", bound=Document)


class BaseRepository(Generic[ModelT]):
    """Repositorio base para Beanie ODM (MongoDB), parametrizado por el modelo.

    OJO — las firmas difieren del repo SQLAlchemy (misma clase, distinto ORM):
      - get por id:  `get_by_id(id)`   (SQL usa `get(id)`)
      - update:      `update(obj, data)` — el 1er arg es el **Document**, no el
                     id (SQL usa `update(id, dict)`). Fetch primero con
                     `get_by_id`, luego `update(obj, data)`.
      - delete:      `delete(obj)` — recibe el Document (SQL usa `delete(id)`).
      - create:      `create(obj | dict)`.
    Listado: `build_list_queryset(...)`→FindMany + `paginate(...)`, o
    `build_list_pipeline(...)` + `paginate_pipeline(...)` para joins (`$lookup`).
    Preferí llamar el SERVICE (API unificada) en vez del repo directo; llamar
    `get_by_id` directo bypassea el scoping de `get_filters()`.
    """

    model: Type[ModelT]

    def _parse_order_field(self, order_by: str) -> tuple[str, int, bool]:
        """Parse order_by string into components.
        
        Args:
            order_by: Order string, e.g., "-created_at" or "tool__name"
            
        Returns:
            tuple: (field_path, direction, is_nested)
            - field_path: "tool.name" or "created_at"
            - direction: 1 (asc) or -1 (desc)
            - is_nested: True if contains "__"
        """
        # Check for descending prefix
        direction = -1 if order_by.startswith("-") else 1
        field = order_by.lstrip("-")
        
        # Check if nested (contains __ or .)
        is_nested = "__" in field or "." in field
        
        # Convert __ to . for MongoDB field path (normalize)
        field_path = field.replace("__", ".")
        
        return field_path, direction, is_nested
    
    def _get_collection_name_from_field(self, field_name: str) -> Optional[str]:
        """Get the collection name for a Link field.
        
        Args:
            field_name: Name of the field in the model
            
        Returns:
            Collection name or None if not a Link field
        """
        if not hasattr(self.model, "model_fields"):
            return None
            
        model_fields = self.model.model_fields
        field_info = model_fields.get(field_name)
        
        if not field_info:
            return None
        
        field_type = field_info.annotation
        origin = get_origin(field_type)
        
        # Handle Link[Model]
        if origin is Link:
            args = get_args(field_type)
            if args:
                linked_model = args[0]
                if hasattr(linked_model, "Settings") and hasattr(linked_model.Settings, "name"):
                    return linked_model.Settings.name
        
        # Handle Optional[Link[Model]]
        if origin is Union:
            args = get_args(field_type)
            for arg in args:
                arg_origin = get_origin(arg)
                if arg_origin is Link:
                    link_args = get_args(arg)
                    if link_args:
                        linked_model = link_args[0]
                        if hasattr(linked_model, "Settings") and hasattr(linked_model.Settings, "name"):
                            return linked_model.Settings.name
        
        return None

    def _get_query_kwargs(
        self,
        fetch_links: bool = False,
        nesting_depths_per_field: Optional[Dict[str, int]] = None,
        projection: Optional[Union[List[str], Type[BaseModel]]] = None,
    ):
        kwargs = {
            "fetch_links": fetch_links,
            "nesting_depths_per_field": (
                nesting_depths_per_field if fetch_links else None
            ),
        }
        if projection is not None:
            kwargs["projection"] = projection
        return kwargs

    def _is_link_field(self, field_name: str) -> bool:
        """Verifica si un campo del modelo es de tipo Link (o Optional[Link])."""
        model_fields = (
            self.model.model_fields
            if hasattr(self.model, "model_fields")
            else {}
        )
        field_info = model_fields.get(field_name)
        if not field_info:
            return False

        field_type = field_info.annotation
        origin = get_origin(field_type)

        # Caso directo: Link[Model]
        if origin is Link:
            return True

        # Caso Optional[Link[Model]] = Union[Link[Model], None]
        # O cualquier Union que contenga Link
        if origin is not None:
            args = get_args(field_type)
            for arg in args:
                arg_origin = get_origin(arg)
                if arg_origin is Link:
                    return True

        return False

    @staticmethod
    def _coerce_objectid(value: Any) -> Any:
        """str/UUID → ObjectId para queries Mongo sobre Link.id."""
        if isinstance(value, ObjectId):
            return value
        if isinstance(value, str):
            try:
                return ObjectId(value)
            except Exception:
                return value
        return value

    def _resolve_filter_query_args(self, filters: dict = None) -> list:
        """Traduce un dict de `get_filters()` a los args posicionales que
        acepta `Model.find()`/`Model.find_one()` — resuelve campos Link
        (directos y el alias `<field>_id`), pasa claves Mongo crudas
        (`"user.$id"`, `"$or"`, ...) como dict, y avisa (no falla en
        silencio) si una clave no resuelve a nada del modelo.

        Compartido por `build_filter_query` (listados) y `get_by_id`
        (retrieve/update/delete escopeados) — mismo motor de resolución de
        filtros para las dos rutas, así un `get_filters()` de seguridad
        protege ambas por igual."""
        exprs: list = []
        raw_filters: Dict[str, Any] = {}

        for k, v in (filters or {}).items():
            # MongoDB-style keys (dot-notation like "user.$id" or operators like "$or")
            # cannot be resolved via hasattr — pass them as a raw dict to find()
            if "." in k or k.startswith("$"):
                raw_filters[k] = v
                continue

            if hasattr(self.model, k):
                field_attr = getattr(self.model, k)
                if self._is_link_field(k):
                    exprs.append(field_attr.id == self._coerce_objectid(v))
                else:
                    exprs.append(field_attr == v)
                continue

            # `<field>_id` alias para Link fields. Permite filtros con
            # `customer_id`, `user_id`, etc. sin que el caller deba conocer
            # la sintaxis Mongo nested. Sólo se activa si <field> existe en
            # el modelo y es un Link[X].
            if k.endswith("_id"):
                base = k[:-3]
                if hasattr(self.model, base) and self._is_link_field(base):
                    field_attr = getattr(self.model, base)
                    exprs.append(field_attr.id == self._coerce_objectid(v))
                    continue

            # Clave no resoluble (typo, campo inexistente, nesting mal escrito).
            # Se descarta — pero se AVISA: si la clave venía de `get_filters`
            # (scoping por tenant/owner), su desaparición silenciosa deja el
            # listado/retrieve SIN ese filtro. El warning hace visible la
            # pérdida en logs/Sentry en vez de fallar en silencio.
            logger.warning(
                "_resolve_filter_query_args: filtro '%s' descartado (no "
                "resuelve a ningún campo de %s). Si era un filtro de "
                "scoping, la query queda SIN ese filtro.",
                k,
                getattr(self.model, "__name__", self.model),
            )

        # Raw MongoDB filters go first so Beanie processes them as a dict condition
        return ([raw_filters] if raw_filters else []) + exprs

    def build_filter_query(
        self,
        search: Optional[str],
        search_fields: List[str],
        filters: dict = None,
        order_by: Optional[List[tuple]] = None,
        **kwargs,
    ) -> FindMany[Document]:
        """Versión personalizada que soporta campos Link."""
        exprs = []

        if search and search_fields:
            exprs.append(
                Or(
                    *[
                        RegEx(
                            getattr(self.model, f),
                            f".*{search}.*",
                            options="i",
                        )
                        for f in search_fields
                    ]
                )
            )

        query_args: list = exprs + self._resolve_filter_query_args(filters)
        query = self.model.find(*query_args, **self._get_query_kwargs(**kwargs))

        # Apply ordering if provided
        if order_by:
            query = query.sort(order_by)

        return query

    async def paginate(
        self, query: FindMany[Document], page: int, count: int, order_by: Optional[List[tuple]] = None
    ) -> tuple[List[Document], int]:
        """MOTOR DE PAGINACIÓN (FindMany) — NO LO REIMPLEMENTES.

        Único loop de paginación offset (count + skip/limit). Para personalizar
        el listado, override el HOOK correcto, no copies skip/limit/count:
        - query/filtros/scoping/orden → `build_list_queryset` (o `build_filter_query`)
        - join `$lookup` / shape plano → `use_aggregation=True` + `build_list_pipeline`
        - filtros por usuario/acción   → `Service.get_filters`
        - orden por defecto            → `Service.get_order`
        - enriquecer items de la página→ `Service.post_process_list`
        - scroll infinito / cursor     → `paginate_keyset` (método+endpoint aparte)
        """
        # Apply ordering if provided and not already applied
        if order_by:
            query = query.sort(order_by)

        total = await query.count()
        items = await query.skip(count * (page - 1)).limit(count).to_list()
        return items, total

    async def paginate_keyset(
        self,
        query: FindMany[Document],
        limit: int,
        cursor_field: str,
        cursor_value: Optional[Any] = None,
        ascending: bool = False,
    ) -> tuple[List[Document], bool]:
        """Keyset (cursor) pagination — O(1) por página, sin `skip` ni `count`.

        Alternativa a `paginate` para listados que escalan: en vez de contar
        toda la colección y saltar `count*(page-1)` documentos (coste que crece
        con la profundidad), filtra por un cursor sobre `cursor_field` y trae
        `limit + 1` para saber si quedan más — sin un `count()`.

        Args:
            query: FindMany ya filtrado (los filtros base del listado).
            limit: tamaño de página.
            cursor_field: campo Mongo indexado del cursor (ej. "_id",
                "created_at", "timestamp"). Debe existir un índice que lo cubra.
            cursor_value: valor del último item de la página previa. None =
                primera página.
            ascending: True = orden ascendente (`> cursor`); False = descendente
                (`< cursor`), útil para "más recientes primero".

        Returns:
            (items, has_more). El caller deriva el próximo cursor del último
            item devuelto (`getattr(items[-1], cursor_field)`).
        """
        limit = max(1, int(limit))
        op = "$gt" if ascending else "$lt"
        if cursor_value is not None:
            query = query.find({cursor_field: {op: cursor_value}})
        query = query.sort((cursor_field, 1 if ascending else -1))
        docs = await query.limit(limit + 1).to_list()
        has_more = len(docs) > limit
        return docs[:limit], has_more

    def build_list_queryset(
        self,
        search: Optional[str] = None,
        search_fields: Optional[List[str]] = None,
        filters: Optional[dict] = None,
        order_by: Optional[List[tuple]] = None,
        **kwargs,
    ) -> FindMany[Document]:
        """Hook: returns the FindMany query used by `list` endpoints.

        Beanie equivalent of SQLAlchemy `build_list_queryset`. Override at
        repository OR service level (`BaseService.build_list_queryset`) to
        customize filters, projections, or query options before pagination.
        Default implementation delegates to `build_filter_query`.
        """
        return self.build_filter_query(
            search=search,
            search_fields=search_fields or [],
            filters=filters or {},
            order_by=order_by,
            **kwargs,
        )

    def _build_match_stage(
        self,
        search: Optional[str],
        search_fields: Optional[List[str]],
        filters: Optional[dict],
    ) -> Dict[str, Any]:
        """Build a `$match` stage dict (without the `$match` wrapper)."""
        match_conditions: Dict[str, Any] = {}
        if filters:
            for key, value in filters.items():
                if isinstance(value, ObjectId):
                    match_conditions[key] = value
                elif hasattr(value, "id"):
                    match_conditions[f"{key}.$id"] = value.id
                else:
                    match_conditions[key] = value

        if search and search_fields:
            search_conditions = [
                {field: {"$regex": f".*{search}.*", "$options": "i"}}
                for field in search_fields
            ]
            if search_conditions:
                if match_conditions:
                    match_conditions = {
                        "$and": [
                            match_conditions,
                            {"$or": search_conditions},
                        ]
                    }
                else:
                    match_conditions = {"$or": search_conditions}
        return match_conditions

    def build_list_pipeline(
        self,
        search: Optional[str] = None,
        search_fields: Optional[List[str]] = None,
        filters: Optional[dict] = None,
        order_by: Optional[str] = None,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        """Hook: returns the aggregation pipeline used by `list` endpoints.

        Beanie equivalent of SQL subqueries / JOINs. Override at repository
        OR service level (`BaseService.build_list_pipeline`) to add `$lookup`,
        `$project`, `$group`, etc. Default builds `$match` + optional `$sort`
        (with auto `$lookup` for nested-link ordering). The `$facet` pagination
        stage is appended later by `paginate_pipeline`.
        """
        pipeline: List[Dict[str, Any]] = []

        match_conditions = self._build_match_stage(search, search_fields, filters)
        if match_conditions:
            pipeline.append({"$match": match_conditions})

        if not order_by:
            return pipeline

        field_path, direction, is_nested = self._parse_order_field(order_by)

        if is_nested:
            parts = field_path.split(".")
            first_field = parts[0]
            collection_name = self._get_collection_name_from_field(first_field)
            if collection_name:
                pipeline.extend([
                    {
                        "$lookup": {
                            "from": collection_name,
                            "localField": f"{first_field}.$id",
                            "foreignField": "_id",
                            "as": f"{first_field}_data",
                        }
                    },
                    {
                        "$unwind": {
                            "path": f"${first_field}_data",
                            "preserveNullAndEmptyArrays": True,
                        }
                    },
                ])
                remaining_path = ".".join(parts[1:]) if len(parts) > 1 else ""
                sort_field = (
                    f"{first_field}_data.{remaining_path}"
                    if remaining_path
                    else f"{first_field}_data"
                )
            else:
                sort_field = field_path
        else:
            sort_field = field_path

        pipeline.append({"$sort": {sort_field: direction}})
        return pipeline

    async def paginate_pipeline(
        self,
        pipeline: List[Dict[str, Any]],
        page: int,
        count: int,
        validate: bool = True,
    ) -> tuple[List[Any], int]:
        """MOTOR DE PAGINACIÓN (aggregation `$facet`) — NO LO REIMPLEMENTES.

        Ejecuta el pipeline con paginación `$facet`. Para un listado agregado
        NO copies este `$facet`/skip/limit: override `Service.build_list_pipeline`
        (agrega `$lookup`/`$project`/`$group`) y activa `use_aggregation=True`;
        este método le pone la paginación. Si la proyección no es el modelo,
        `aggregation_validate=False`.

        Args:
            pipeline: Pipeline stages (without the final `$facet`).
            page, count: Pagination params.
            validate: If True, validates each row against `self.model`.
                Set False when the pipeline projects a non-model shape (e.g.
                joined columns) — the raw dicts are returned untouched.
        """
        full_pipeline = list(pipeline) + [
            {
                "$facet": {
                    "metadata": [{"$count": "total"}],
                    "data": [
                        {"$skip": count * (page - 1)},
                        {"$limit": count},
                    ],
                }
            }
        ]

        results = await self.model.aggregate(full_pipeline).to_list()
        if not results or not results[0].get("metadata"):
            return [], 0

        data = results[0]
        total = data["metadata"][0]["total"] if data["metadata"] else 0
        items_raw = data["data"]

        if not validate:
            return items_raw, total

        items: List[Any] = []
        dropped = 0
        for raw_item in items_raw:
            try:
                items.append(self.model.model_validate(raw_item))
            except Exception as exc:
                # No tragar en silencio: una fila que no valida se pierde de la
                # respuesta sin aviso (menos items que el limit, total inflado).
                # Se loguea para que sea diagnosticable; la fila igual se omite
                # para no romper el listado completo por un doc corrupto.
                dropped += 1
                logger.warning(
                    "paginate_pipeline: fila descartada por fallo de "
                    "validación contra %s: %s",
                    getattr(self.model, "__name__", self.model),
                    exc,
                )
        if dropped:
            logger.warning(
                "paginate_pipeline: %d/%d filas descartadas en %s "
                "(total reportado=%d)",
                dropped,
                len(items_raw),
                getattr(self.model, "__name__", self.model),
                total,
            )
        return items, total

    async def list_with_aggregation(
        self,
        search: Optional[str],
        search_fields: List[str],
        filters: dict,
        order_by: str,
        page: int,
        count: int,
        **kwargs,
    ) -> tuple[List[Document], int]:
        """Backward-compat wrapper: delegates to `build_list_pipeline` +
        `paginate_pipeline`. Kept for callers that hit it directly.
        """
        pipeline = self.build_list_pipeline(
            search=search,
            search_fields=search_fields,
            filters=filters,
            order_by=order_by,
            **kwargs,
        )
        # Strip join artifacts when default sort-lookup added them
        field_path, _, is_nested = self._parse_order_field(order_by) if order_by else ("", 0, False)
        if is_nested:
            first_field = field_path.split(".")[0]
            if self._get_collection_name_from_field(first_field):
                pipeline.append({"$project": {f"{first_field}_data": 0}})
        return await self.paginate_pipeline(pipeline, page, count, validate=True)

    async def get_by_id(
        self,
        obj_id: Union[str, ObjectId],
        filters: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> Optional[ModelT]:
        """`filters` (opcional) — scoping de seguridad (típicamente
        `Service.get_filters()`, ej. `{"user.$id": request.state.user.id}`).
        Sin `filters`, comportamiento idéntico a antes (busca por id, sin
        scope) — 100% retrocompatible. Con `filters`, un id que existe pero
        no matchea el scope devuelve `None` (mismo resultado que "no
        encontrado"), cerrando la clase de IDOR de "conozco el id de otro
        tenant y lo pido directo".

        El chequeo de scope corre SIEMPRE con `fetch_links=False` (find_one
        plano, sobre el documento crudo — DBRef sin resolver), sea cual sea
        el `fetch_links` que pida el caller para el fetch final. Motivo:
        Beanie reescribe `find(..., fetch_links=True)` a una aggregation con
        `$lookup`, y un filtro con notación de punto (`"user.$id"`,
        `"user._id"`, etc.) puede matchear distinto — o no matchear nada —
        según si corre ANTES o DESPUÉS del `$lookup` (mismo bug ya
        documentado en `find_all_production_for_user` de pulbot-backend:
        "fetch_links=True puede descartar resultados silenciosamente al
        filtrar por campos dict anidados"). Combinar scope-filter +
        fetch_links en una sola query es frágil; separarlos lo hace
        determinístico sin importar qué `get_filters()` use cada service."""
        if not isinstance(obj_id, ObjectId):
            obj_id = ObjectId(obj_id)

        if filters:
            scope_exprs = self._resolve_filter_query_args(filters)
            scoped = await self.model.find_one(
                self.model.id == obj_id, *scope_exprs, fetch_links=False
            )
            if not scoped:
                return None

        return await self.model.find_one(
            self.model.id == obj_id,
            **self._get_query_kwargs(**kwargs),
        )

    async def get(
        self, obj_id: Union[str, ObjectId], **kwargs
    ) -> Optional[ModelT]:
        """Alias de `get_by_id(id)` — nombre unificado con los repos SQL
        (`get`) para que las lecturas por id sean portables entre ORMs."""
        return await self.get_by_id(obj_id, **kwargs)

    async def get_by_field(
        self,
        field_name: str,
        value: Any,
        **kwargs,
    ) -> Optional[ModelT]:
        if not hasattr(self.model, field_name):
            raise AttributeError(
                f"{self.model.__name__} no tiene el campo '{field_name}'"
            )
        return await self.model.find_one(
            getattr(self.model, field_name) == value,
            **self._get_query_kwargs(**kwargs),
        )

    async def get_by_fields(
        self,
        filters: Dict[str, Any],
        **kwargs,
    ) -> Optional[ModelT]:
        exprs = [
            getattr(self.model, f) == v
            for f, v in filters.items()
            if hasattr(self.model, f)
        ]
        if not exprs:
            return None
        return await self.model.find_one(
            *exprs, **self._get_query_kwargs(**kwargs)
        )

    async def list_all(
        self,
        **kwargs,
    ) -> List[ModelT]:
        query = self.model.find_all(**self._get_query_kwargs(**kwargs))
        return await query.to_list()

    async def create(self, obj: Union[ModelT, Dict[str, Any]]) -> ModelT:
        if isinstance(obj, dict):
            obj = self.model(**obj)
        await obj.insert()
        return obj

    async def update(self, obj: ModelT, data: Dict[str, Any]) -> ModelT:
        """Actualiza un Document Beanie ya cargado.

        Divergencia con SQL: acá el 1er arg es el **Document** (no el id) y SÍ
        setea ``None``. El repo SQLAlchemy usa ``update(id, dict)`` y omite
        ``None``. Ver la tabla en el CLAUDE.md de la lib.
        """
        for key, value in data.items():
            setattr(obj, key, value)
        await obj.save()
        return obj

    async def delete(self, obj: ModelT) -> None:
        await obj.delete()
