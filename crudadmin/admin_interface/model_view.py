import datetime
from collections.abc import AsyncGenerator, Callable, Coroutine
from datetime import datetime as dt
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Set,
    Type,
    TypeVar,
    Union,
    cast,
)
from uuid import UUID

from fastapi import APIRouter, Depends, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from fastcrud import EndpointCreator, FastCRUD
from pydantic import BaseModel, ValidationError
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase

from ..core.db import DatabaseConfig
from ..event import EventType, log_admin_action
from .helper import _get_form_fields_from_schema

EndpointCallable = Callable[..., Coroutine[Any, Any, Response]]

ModelType = TypeVar("ModelType", bound=DeclarativeBase)
CreateSchemaType = TypeVar("CreateSchemaType", bound=BaseModel)
UpdateSchemaType = TypeVar("UpdateSchemaType", bound=BaseModel)
UpdateSchemaInternalType = TypeVar("UpdateSchemaInternalType", bound=BaseModel)
DeleteSchemaType = TypeVar("DeleteSchemaType", bound=BaseModel)
SelectSchemaType = TypeVar("SelectSchemaType", bound=BaseModel)


class BulkDeleteRequest(BaseModel):
    """Request model for bulk delete operations containing IDs to delete."""

    ids: List[Union[int, str]]


class PasswordTransformer:
    """
    Configuration for transforming password fields in forms.

    This allows models to have different field names and hashing methods
    for password handling.
    """

    def __init__(
        self,
        password_field: str = "password",
        hashed_field: str = "hashed_password",
        hash_function: Optional[Callable[[str], str]] = None,
        required_fields: Optional[List[str]] = None,
    ):
        """
        Initialize password transformer.

        Args:
            password_field: Name of the password field in the form/schema
            hashed_field: Name of the hashed password field in the internal schema/model
            hash_function: Function to hash passwords (takes string, returns string)
            required_fields: List of other required fields that must be present
        """
        self.password_field = password_field
        self.hashed_field = hashed_field
        self.hash_function = hash_function
        self.required_fields = required_fields or []

    def transform_create_data(
        self, form_data: Dict[str, Any], item_data: BaseModel
    ) -> Dict[str, Any]:
        """
        Transform form data for create operations.

        Args:
            form_data: Raw form data dictionary
            item_data: Validated schema instance

        Returns:
            Dictionary with transformed data for internal schema
        """
        transformed_data = {}

        for field_name, field_value in form_data.items():
            if field_name != self.password_field:
                transformed_data[field_name] = field_value

        password = getattr(item_data, self.password_field, None)
        if password is not None and self.hash_function:
            transformed_data[self.hashed_field] = self.hash_function(password)
        elif password is not None:
            transformed_data[self.hashed_field] = password

        return transformed_data

    def transform_update_data(
        self, form_data: Dict[str, Any], item_data: BaseModel
    ) -> Dict[str, Any]:
        """
        Transform form data for update operations.

        Args:
            form_data: Raw form data dictionary
            item_data: Validated schema instance

        Returns:
            Dictionary with transformed data for internal schema
        """
        transformed_data = {"updated_at": dt.now(datetime.UTC)}

        for field_name, field_value in form_data.items():
            if (
                field_name != self.password_field
                and field_value
                and field_name != "updated_at"
            ):
                transformed_data[field_name] = field_value

        password = getattr(item_data, self.password_field, None)
        if password is not None and self.hash_function:
            transformed_data[self.hashed_field] = self.hash_function(password)  # type: ignore[assignment]
        elif password is not None:
            transformed_data[self.hashed_field] = str(password)  # type: ignore[assignment]

        return transformed_data


class ModelView:
    r"""
    View class for managing CRUD operations and UI for database models in FastAPI admin interface.

    Features:
        - Automatic form generation from Pydantic schemas
        - List view with pagination, sorting, and filtering
        - Create/update forms with validation
        - Bulk delete operations
        - Event logging integration
        - HTMX-powered dynamic updates

    Args:
        database_config: DatabaseConfig instance for DB connections
        templates: Jinja2Templates instance for rendering views
        model: SQLAlchemy model class to manage
        allowed_actions: Set of allowed operations ('view', 'create', 'update', 'delete')
        create_schema: Pydantic schema for create operations
        update_schema: Pydantic schema for update operations
        update_internal_schema: Optional internal schema for special update cases
        delete_schema: Optional schema for delete operations
        select_schema: Optional schema for select operations
        admin_model: Whether this is an admin-specific model
        admin_site: Reference to parent AdminSite instance
        event_integration: Optional event logging integration
        password_transformer: Optional password transformer for AdminUser model

    Raises:
        ValueError: If schemas don't match model structure
        TypeError: If model is not a SQLAlchemy model
        RuntimeError: If required dependencies are missing

    Notes:
        - Forms are auto-generated based on Pydantic schema definitions
        - List views support server-side pagination and filtering
        - Changes are tracked if event logging is enabled
        - HTMX is used for dynamic content updates
        - Templates can be customized by overriding defaults

    URLs Generated:
        **List View:**
            GET /{model_name}/ - Main list view with pagination
            GET /{model_name}/get_model_list - HTMX-powered list content

        **Create:**
            GET /{model_name}/create_page - Create form
            POST /{model_name}/form_create - Handle create submission

        **Update:**
            GET /{model_name}/update/{id} - Update form for specific record
            POST /{model_name}/form_update/{id} - Handle update submission

        **Delete:**
            DELETE /{model_name}/bulk-delete - Bulk delete selected records

        **API Endpoints:**
            All CRUD operations also exposed as REST API endpoints under /crud/

    Example:
        Basic model view setup:
        ```python
        from pydantic import BaseModel, Field
        from sqlalchemy import Column, Integer, String
        from sqlalchemy.ext.declarative import DeclarativeBase

        # Define model
        class User(DeclarativeBase):
            __tablename__ = "users"
            id = Column(Integer, primary_key=True)
            username = Column(String, unique=True)
            email = Column(String)
            role = Column(String)

        # Define schemas
        class UserCreate(BaseModel):
            username: str = Field(..., min_length=3)
            email: str = Field(..., pattern=r"[^@]+@[^@]+\.[^@]+")
            role: str = Field(default="user")

        class UserUpdate(BaseModel):
            email: Optional[str] = Field(None, pattern=r"[^@]+@[^@]+\.[^@]+")
            role: Optional[str] = None

        # Create view
        user_view = ModelView(
            database_config=db_config,
            templates=templates,
            model=User,
            create_schema=UserCreate,
            update_schema=UserUpdate,
            allowed_actions={"view", "create", "update"}
        )
        ```

        Custom form validation:
        ```python
        from datetime import datetime
        from decimal import Decimal
        from typing import Optional
        from pydantic import BaseModel, Field, validator

        class OrderCreate(BaseModel):
            customer_id: int
            total: Decimal = Field(..., ge=0)
            status: str = Field(default="pending")
            notes: Optional[str] = None

            @validator("total")
            def validate_total(cls, v):
                if v > 1000000:
                    raise ValueError("Order total cannot exceed 1,000,000")
                return v

            @validator("status")
            def validate_status(cls, v):
                allowed = {"pending", "paid", "shipped", "cancelled"}
                if v not in allowed:
                    raise ValueError(f"Status must be one of: {allowed}")
                return v

        class OrderUpdate(BaseModel):
            status: Optional[str] = None
            notes: Optional[str] = None

            @validator("status")
            def validate_status(cls, v):
                if v is not None:
                    allowed = {"pending", "paid", "shipped", "cancelled"}
                    if v not in allowed:
                        raise ValueError(f"Status must be one of: {allowed}")
                return v

        order_view = ModelView(
            database_config=db_config,
            templates=templates,
            model=Order,
            create_schema=OrderCreate,
            update_schema=OrderUpdate,
            allowed_actions={"view", "create", "update"}
        )
        ```

        Event logging integration:
        ```python
        from typing import Optional
        from datetime import datetime
        from pydantic import BaseModel, Field

        class ProductCreate(BaseModel):
            name: str
            price: float = Field(..., gt=0)
            stock: int = Field(..., ge=0)

        class ProductUpdate(BaseModel):
            name: Optional[str] = None
            price: Optional[float] = Field(None, gt=0)
            stock: Optional[int] = Field(None, ge=0)

        # With event logging
        product_view = ModelView(
            database_config=db_config,
            templates=templates,
            model=Product,
            create_schema=ProductCreate,
            update_schema=ProductUpdate,
            event_integration=event_logger,  # Enable logging
            allowed_actions={"view", "create", "update", "delete"}
        )

        # Events logged:
        # - Record creation with user info
        # - Updates with change details
        # - Deletions with record info
        # - View access for audit trails
        ```

        Custom templates:
        ```python
        templates = Jinja2Templates(directory="custom_templates")

        # Override default templates
        custom_templates = {
            "list": "custom/model/list.html",
            "create": "custom/model/create.html",
            "update": "custom/model/update.html"
        }

        view = ModelView(
            database_config=db_config,
            templates=templates,  # Custom templates
            model=User,
            create_schema=UserCreate,
            update_schema=UserUpdate,
            allowed_actions={"view", "create", "update"}
        )
        ```

        Restricted actions:
        ```python
        # Read-only view
        readonly_view = ModelView(
            database_config=db_config,
            templates=templates,
            model=AuditLog,
            create_schema=AuditLogSchema,
            update_schema=AuditLogSchema,
            allowed_actions={"view"}  # View only
        )

        # No delete view
        no_delete_view = ModelView(
            database_config=db_config,
            templates=templates,
            model=Customer,
            create_schema=CustomerCreate,
            update_schema=CustomerUpdate,
            allowed_actions={"view", "create", "update"}  # No delete
        )
        ```
    """

    def __init__(
        self,
        database_config: DatabaseConfig,
        templates: Jinja2Templates,
        model: Type[DeclarativeBase],
        allowed_actions: Set[str],
        create_schema: Type[CreateSchemaType],
        update_schema: Type[UpdateSchemaType],
        update_internal_schema: Optional[Type[UpdateSchemaInternalType]] = None,
        delete_schema: Optional[Type[DeleteSchemaType]] = None,
        select_schema: Optional[Type[SelectSchemaType]] = None,
        admin_model: bool = False,
        admin_site: Optional[Any] = None,
        event_integration: Optional[Any] = None,
        password_transformer: Optional[PasswordTransformer] = None,
    ) -> None:
        self.db_config = database_config
        self.templates = templates
        self.model = model
        self.model_key = model.__name__
        self.router = APIRouter()
        self.admin_model = admin_model
        self.admin_site = admin_site
        self.allowed_actions = allowed_actions
        self.event_integration = event_integration
        self.password_transformer = password_transformer

        get_session: Callable[[], AsyncGenerator[AsyncSession, None]]
        if self._model_is_admin_model(model):
            get_session = self.db_config.get_admin_db
        else:
            get_session = cast(
                Callable[[], AsyncGenerator[AsyncSession, None]], self.db_config.session
            )
        self.session = get_session

        self.create_schema = create_schema
        self.update_schema = update_schema
        self.update_internal_schema = update_internal_schema
        self.delete_schema = delete_schema
        self.select_schema = select_schema

        self.user_service = (
            self.admin_site.admin_user_service if self.admin_site else None
        )

        if self.model.__name__ == "AdminUser" and password_transformer is None:
            from ..core.auth import get_password_hash

            self.password_transformer = PasswordTransformer(
                password_field="password",
                hashed_field="hashed_password",
                hash_function=get_password_hash,
                required_fields=["username"],
            )

        self.crud: FastCRUD[Any, Any, Any, Any, Any, Any] = FastCRUD(self.model)

        self.endpoints_template = EndpointCreator(
            session=self.session,
            model=self.model,
            crud=self.crud,
            create_schema=self.create_schema,
            update_schema=self.update_schema,
            delete_schema=self.delete_schema,
        )
        self.endpoints_template.add_routes_to_router()
        self.router.include_router(self.endpoints_template.router, prefix="/crud")

        self.setup_routes()

    def get_url_prefix(self) -> str:
        """Get the URL prefix for admin routes, handling root mount path correctly."""
        if self.admin_site:
            return (
                f"/{self.admin_site.mount_path}" if self.admin_site.mount_path else ""
            )
        return ""

    def _model_is_admin_model(self, model: Type[DeclarativeBase]) -> bool:
        """Check if a model is considered an admin model."""
        return self.admin_model or self.model_key.lower() in {"adminuser", "admin_user"}

    def _convert_id_to_pk_type(
        self, id_value: Union[int, str]
    ) -> Union[int, str, float]:
        """Convert the ID value to the appropriate type based on the model's primary key type."""
        if id_value is None:
            return None

        primary_key_info = self.db_config.get_primary_key_info(self.model)
        if not primary_key_info:
            return id_value

        pk_type = primary_key_info.get("type")

        if pk_type is int:
            return int(id_value) if isinstance(id_value, str) else id_value
        elif pk_type is str:
            return str(id_value)
        elif pk_type is float:
            return float(id_value) if isinstance(id_value, str) else id_value
        elif pk_type is UUID:
            return str(id_value)
        else:
            return str(id_value)

    def setup_routes(self) -> None:
        """
        Configure FastAPI routes based on allowed actions.

        Sets up the following routes if allowed:
        - Create: /form_create (POST), /create_page (GET)
        - View: / (GET), /get_model_list (GET)
        - Delete: /bulk-delete (DELETE)
        - Update: /update/{id} (GET), /form_update/{id} (POST)

        Routes are configured based on the allowed_actions set provided during initialization.
        All routes use appropriate templates and include required dependencies.

        Example:
            ```python
            # Configure with specific actions
            view = ModelView(
                allowed_actions={"view", "create", "update"},
                ...
            )
            view.setup_routes()  # Only creates view/create/update routes
            ```
        """
        if "create" in self.allowed_actions:
            self.router.add_api_route(
                "/form_create",
                self.form_create_endpoint(template="admin/model/create.html"),
                methods=["POST"],
                include_in_schema=False,
                response_model=None,
            )
            self.router.add_api_route(
                "/create_page",
                self.get_model_create_page(template="admin/model/create.html"),
                methods=["GET"],
                include_in_schema=False,
                response_model=None,
            )

        if "view" in self.allowed_actions:
            self.router.add_api_route(
                "/",
                self.get_model_admin_page(),
                methods=["GET"],
                include_in_schema=False,
                response_model=None,
            )
            self.router.add_api_route(
                "/get_model_list",
                self.get_model_admin_page(
                    template="admin/model/components/list_content.html"
                ),
                methods=["GET"],
                include_in_schema=False,
                response_model=None,
            )

        if "delete" in self.allowed_actions:
            self.router.add_api_route(
                "/bulk-delete",
                self.bulk_delete_endpoint(),
                methods=["DELETE"],
                include_in_schema=False,
                response_model=None,
            )

        if "update" in self.allowed_actions:
            self.router.add_api_route(
                "/update/{id}",
                self.get_model_update_page(template="admin/model/update.html"),
                methods=["GET"],
                include_in_schema=False,
                response_model=None,
            )
            self.router.add_api_route(
                "/form_update/{id}",
                self.form_update_endpoint(),
                methods=["POST"],
                include_in_schema=False,
                response_model=None,
            )

    def form_create_endpoint(self, template: str) -> EndpointCallable:
        """
        Create endpoint for handling form submissions to create new model records.

        Args:
            template: Path to Jinja2 template for rendering form

        Returns:
            FastAPI route handler for create form submission

        Features:
            - Form data validation using create_schema
            - Special handling for AdminUser model
            - File upload support
            - Field error collection
            - Event logging integration
            - HTMX support for dynamic updates

        Notes:
            - Uses @log_admin_action decorator for event tracking
            - Handles both single and multi-value form fields
            - Supports password hashing for AdminUser model

        Example:
            ```python
            endpoint = view.form_create_endpoint("admin/model/create.html")
            router.add_api_route("/create", endpoint, methods=["POST"])
            ```
        """

        @log_admin_action(EventType.CREATE, model=self.model)
        async def form_create_endpoint_inner(
            request: Request,
            db: AsyncSession = Depends(self.session),
            admin_db: AsyncSession = Depends(self.db_config.get_admin_db),
            current_user: dict = Depends(
                cast(Any, self.admin_site).admin_authentication.get_current_user()
            ),
            event_integration=Depends(lambda: self.event_integration),
        ) -> Response:
            """Handle POST form submission to create a model record."""
            assert self.admin_site is not None

            form_fields = _get_form_fields_from_schema(self.create_schema)
            error_message: Optional[str] = None
            field_errors: Dict[str, str] = {}
            field_values: Dict[str, Any] = {}

            try:
                if request.method == "POST":
                    form_data_raw = await request.form()
                    form_data: Dict[str, Any] = {}

                    for field in form_fields:
                        key = field["name"]
                        raw_value = form_data_raw.getlist(key)
                        if len(raw_value) == 1:
                            value = raw_value[0]
                            form_data[key] = value if value else field.get("default")
                            field_values[key] = value
                        elif len(raw_value) > 1:
                            form_data[key] = raw_value
                            field_values[key] = raw_value
                        else:
                            form_data[key] = field.get("default")

                    try:
                        if self.password_transformer is not None:
                            item_data = self.create_schema(**form_data)

                            transformed_data = (
                                self.password_transformer.transform_create_data(
                                    form_data, item_data
                                )
                            )

                            for (
                                required_field
                            ) in self.password_transformer.required_fields:
                                if (
                                    required_field not in transformed_data
                                    or not transformed_data[required_field]
                                ):
                                    raise ValueError(
                                        f"{self.model.__name__} requires a {required_field}."
                                    )

                            if self.model.__name__ == "AdminUser":
                                from ..admin_user.schemas import AdminUserCreateInternal

                                admin_internal_data: AdminUserCreateInternal = (
                                    AdminUserCreateInternal(**transformed_data)
                                )
                                result = await self.crud.create(
                                    db=db, object=admin_internal_data
                                )
                            else:
                                if self.update_internal_schema:
                                    generic_internal_data = self.update_internal_schema(
                                        **transformed_data
                                    )
                                    result = await self.crud.create(
                                        db=db, object=generic_internal_data
                                    )
                                else:
                                    dynamic_internal_data = type(
                                        "InternalSchema", (BaseModel,), {}
                                    )(**transformed_data)
                                    result = await self.crud.create(
                                        db=db, object=dynamic_internal_data
                                    )

                            await db.commit()
                        else:
                            item_data = self.create_schema(**form_data)
                            result = await self.crud.create(db=db, object=item_data)
                            await db.commit()

                        if result:
                            request.state.crud_result = result
                            model_list_url = (
                                f"{self.get_url_prefix()}/{self.model.__name__}/"
                            )
                            if "HX-Request" in request.headers:
                                return RedirectResponse(
                                    url=model_list_url,
                                    headers={"HX-Redirect": model_list_url},
                                )
                            return RedirectResponse(
                                url=model_list_url,
                                status_code=303,
                            )

                    except ValidationError as e:
                        field_errors = {
                            str(err["loc"][0]): err["msg"] for err in e.errors()
                        }
                        error_message = "Please correct the errors below."
                    except Exception as e:
                        error_message = str(e)

            except Exception as e:
                error_message = str(e)

            context = {
                "request": request,
                "model_name": self.model_key,
                "form_fields": form_fields,
                "error": error_message,
                "field_errors": field_errors,
                "field_values": field_values,
                "url_prefix": self.get_url_prefix(),
            }

            return self.templates.TemplateResponse(
                template, context, status_code=422 if error_message else 200
            )

        return cast(EndpointCallable, form_create_endpoint_inner)

    def bulk_delete_endpoint(self) -> EndpointCallable:
        """
        Create endpoint for bulk deletion of model records.

        Returns:
            FastAPI route handler for bulk delete operations

        Features:
            - Handles multiple record deletion in one request
            - Supports different primary key types (int, str, float)
            - Validates IDs before deletion
            - Handles pagination after deletion
            - Event logging integration
            - Transaction management

        Notes:
            - Expects JSON payload with "ids" list
            - Performs type conversion based on primary key type
            - Maintains pagination state after deletion
            - Rolls back transaction on error

        Example:
            ```python
            # Delete multiple records
            await client.delete("/bulk-delete", json={"ids": [1, 2, 3]})
            ```

        Response Formats:
            **Success:**
                - Returns updated list content template
                - Status: 200 OK

            **Errors:**
                - 400: No IDs provided
                - 422: Invalid ID format
                - 400: Database error during deletion
        """

        @log_admin_action(EventType.DELETE, model=self.model)
        async def bulk_delete_endpoint_inner(
            request: Request,
            db: AsyncSession = Depends(self.session),
            admin_db: AsyncSession = Depends(self.db_config.get_admin_db),
            current_user: dict = Depends(
                cast(Any, self.admin_site).admin_authentication.get_current_user()
            ),
            event_integration=Depends(lambda: self.event_integration),
        ) -> Response:
            """Handle bulk deletion of model instances using JSON list of IDs."""
            assert self.admin_site is not None
            try:
                body = await request.json()

                page_str = request.query_params.get("page", "1")
                rows_str = request.query_params.get("rows-per-page-select", "10")
                page = int(page_str)
                rows_per_page = int(rows_str)

                ids = body.get("ids", [])
                if not ids:
                    return JSONResponse(
                        status_code=400,
                        content={
                            "detail": [{"message": "No IDs provided for deletion"}]
                        },
                    )

                inspector = inspect(self.model)
                primary_key = inspector.primary_key[0]
                pk_name = primary_key.name
                pk_type = primary_key.type.python_type

                valid_ids: List[Union[int, str, float]] = []
                for id_value in ids:
                    try:
                        if pk_type is int:
                            valid_ids.append(int(id_value))
                        elif pk_type is str:
                            valid_ids.append(str(id_value))
                        elif pk_type is float:
                            valid_ids.append(float(id_value))
                        else:
                            valid_ids.append(id_value)
                    except (ValueError, TypeError):
                        return JSONResponse(
                            status_code=422,
                            content={
                                "detail": [{"message": f"Invalid ID value: {id_value}"}]
                            },
                        )

                filter_criteria: Dict[str, List[Union[int, str, float]]] = {
                    f"{pk_name}__in": valid_ids
                }
                records_to_delete = await self.crud.get_multi(
                    db=db,
                    limit=len(valid_ids),
                    schema_to_select=self.select_schema,
                    **cast(Any, filter_criteria),
                )

                request.state.deleted_records = records_to_delete.get("data", [])

                try:
                    for id_value in valid_ids:
                        await self.crud.delete(
                            db=db,
                            db_row=None,
                            commit=False,
                            allow_multiple=False,
                            **{pk_name: id_value},
                        )
                    await db.commit()
                except Exception as e:
                    await db.rollback()
                    return JSONResponse(
                        status_code=400,
                        content={
                            "detail": [{"message": f"Error during deletion: {str(e)}"}]
                        },
                    )

                total_count = await self.crud.count(db=db)
                max_page = (total_count + rows_per_page - 1) // rows_per_page
                adjusted_page = min(page, max(1, max_page))

                items_result = await self.crud.get_multi(
                    db=db,
                    offset=(adjusted_page - 1) * rows_per_page,
                    limit=rows_per_page,
                    schema_to_select=self.select_schema,
                )

                items: Dict[str, Any] = {
                    "data": items_result.get("data", []),
                    "total_count": items_result.get("total_count", 0),
                }

                table_columns = [column.key for column in self.model.__table__.columns]
                primary_key_info = self.db_config.get_primary_key_info(self.model)

                context: Dict[str, Any] = {
                    "request": request,
                    "model_items": items["data"],
                    "model_name": self.model_key,
                    "table_columns": table_columns,
                    "total_items": items["total_count"],
                    "current_page": adjusted_page,
                    "rows_per_page": rows_per_page,
                    "primary_key_info": primary_key_info,
                    "url_prefix": self.get_url_prefix(),
                }

                return self.templates.TemplateResponse(
                    "admin/model/components/list_content.html", context
                )

            except ValueError as e:
                return JSONResponse(
                    status_code=422, content={"detail": [{"message": str(e)}]}
                )
            except Exception as e:
                return JSONResponse(
                    status_code=422,
                    content={
                        "detail": [{"message": f"Error processing request: {str(e)}"}]
                    },
                )

        return cast(EndpointCallable, bulk_delete_endpoint_inner)

    def get_model_admin_page(
        self, template: str = "admin/model/list.html"
    ) -> EndpointCallable:
        """
        Create endpoint for model list view with filtering and pagination.

        Args:
            template: Path to Jinja2 template for rendering list view

        Returns:
            FastAPI route handler for model list page

        Example:
            ```python
            # Basic list view
            response = await client.get("/?page=1&rows-per-page-select=25")

            # Sorted and filtered
            response = await client.get(
                "/?sort_by=username&sort_order=desc&column-to-search=email&search-input=example.com"
            )
            ```
        """

        async def get_model_admin_page_inner(
            request: Request,
            admin_db: AsyncSession = Depends(self.db_config.get_admin_db),
            app_db: AsyncSession = Depends(
                cast(
                    Callable[..., AsyncGenerator[AsyncSession, None]],
                    self.db_config.get_app_session(),
                )
            ),
        ) -> Response:
            """Display the model list page, allowing pagination, sorting, and searching."""
            if self._model_is_admin_model(self.model):
                db = admin_db
            else:
                db = app_db

            if template == "admin/model/list.html" and not request.url.path.endswith(
                "/"
            ):
                redirect_url = request.url.path + "/"
                if request.url.query:
                    redirect_url += "?" + request.url.query
                return RedirectResponse(redirect_url, status_code=307)

            try:
                page = max(1, int(request.query_params.get("page", "1")))
                rows_per_page = int(
                    request.query_params.get("rows-per-page-select", "10")
                )
            except ValueError:
                page = 1
                rows_per_page = 10

            sort_column = request.query_params.get("sort_by")
            sort_order = request.query_params.get("sort_order", "asc")

            sort_columns = (
                [sort_column] if sort_column and sort_column != "None" else None
            )
            sort_orders = [sort_order] if sort_order and sort_order != "None" else None

            search_column = request.query_params.get("column-to-search")
            search_value = request.query_params.get("search-input", "").strip()

            filter_criteria: Dict[str, Any] = {}
            if search_column and search_value:
                column = self.model.__table__.columns.get(search_column)
                if column is not None:
                    python_type = column.type.python_type
                    try:
                        if python_type is int:
                            filter_criteria[search_column] = int(search_value)
                        elif python_type is float:
                            filter_criteria[search_column] = float(search_value)
                        elif python_type is bool:
                            lower_search = search_value.lower()
                            if lower_search in ("true", "yes", "1", "t", "y"):
                                filter_criteria[search_column] = True
                            elif lower_search in ("false", "no", "0", "f", "n"):
                                filter_criteria[search_column] = False
                        elif python_type is str:
                            filter_criteria[f"{search_column}__ilike"] = (
                                f"%{search_value}%"
                            )
                    except (ValueError, TypeError):
                        pass

            try:
                total_items = await self.crud.count(db=db, **cast(Any, filter_criteria))
                max_page = max(1, (total_items + rows_per_page - 1) // rows_per_page)
                page = min(page, max_page)
                offset = (page - 1) * rows_per_page

                items_result = await self.crud.get_multi(
                    db=db,
                    offset=offset,
                    limit=rows_per_page,
                    sort_columns=sort_columns,
                    sort_orders=sort_orders,
                    schema_to_select=self.select_schema,
                    **cast(Any, filter_criteria),
                )

                items: Dict[str, Any] = {
                    "data": items_result.get("data", []),
                    "total_count": items_result.get("total_count", 0),
                }

            except Exception:
                items = {"data": [], "total_count": 0}
                total_items = 0
                page = 1

            table_columns = [column.key for column in self.model.__table__.columns]
            primary_key_info = self.db_config.get_primary_key_info(self.model)

            context: Dict[str, Any] = {
                "request": request,
                "model_items": items["data"],
                "model_name": self.model_key,
                "table_columns": table_columns,
                "total_items": items["total_count"],
                "current_page": page,
                "rows_per_page": rows_per_page,
                "selected_column": search_column,
                "primary_key_info": primary_key_info,
                "url_prefix": self.get_url_prefix(),
                "sort_column": sort_column,
                "sort_order": sort_order,
                "allowed_actions": self.allowed_actions,
            }

            if "HX-Request" in request.headers:
                return self.templates.TemplateResponse(
                    "admin/model/components/list_content.html", context
                )

            if self.admin_site is not None:
                base_context = await self.admin_site.get_base_context(
                    admin_db=admin_db, app_db=app_db
                )
                context.update(base_context)
                context["include_sidebar_and_header"] = True

            return self.templates.TemplateResponse(template, context)

        return cast(EndpointCallable, get_model_admin_page_inner)

    def get_model_create_page(
        self, template: str = "admin/model/create.html"
    ) -> EndpointCallable:
        """
        Create endpoint for displaying new record creation form.

        Args:
            template: Path to Jinja2 template for rendering create form

        Returns:
            FastAPI route handler for create form page

        Example:
            ```python
            endpoint = view.get_model_create_page("admin/model/create.html")
            router.add_api_route("/create", endpoint, methods=["GET"])
            ```
        """

        async def model_create_page(request: Request) -> Response:
            """Show a blank form for creating a new record."""
            form_fields = _get_form_fields_from_schema(self.create_schema)
            return self.templates.TemplateResponse(
                template,
                {
                    "request": request,
                    "model_name": self.model_key,
                    "form_fields": form_fields,
                    "url_prefix": self.get_url_prefix(),
                },
            )

        return cast(EndpointCallable, model_create_page)

    def get_model_update_page(self, template: str) -> EndpointCallable:
        """
        Create endpoint for displaying record update form.

        Args:
            template: Path to Jinja2 template for rendering update form

        Returns:
            FastAPI route handler for update form page

        Example:
            ```python
            endpoint = view.get_model_update_page("admin/model/update.html")
            router.add_api_route("/update/{id}", endpoint, methods=["GET"])
            ```
        """

        async def get_model_update_page_inner(
            request: Request,
            id: Union[int, str],
            db: AsyncSession = Depends(self.session),
        ) -> Response:
            """Show a form to update an existing record by `id`."""
            converted_id = self._convert_id_to_pk_type(id)

            item = await self.crud.get(
                db=db, id=converted_id, schema_to_select=self.select_schema
            )
            if not item:
                return JSONResponse(
                    status_code=404, content={"message": f"Item with id {id} not found"}
                )

            form_fields = _get_form_fields_from_schema(self.update_schema)
            for field in form_fields:
                field_name = field["name"]
                if field_name in item:
                    field["value"] = item[field_name]

            return self.templates.TemplateResponse(
                template,
                {
                    "request": request,
                    "model_name": self.model_key,
                    "form_fields": form_fields,
                    "url_prefix": self.get_url_prefix(),
                    "id": id,
                },
            )

        return cast(EndpointCallable, get_model_update_page_inner)

    def form_update_endpoint(self) -> EndpointCallable:
        """
        Create endpoint for handling form submissions to update existing records.

        Returns:
            FastAPI route handler for update form submission

        Notes:
            - Uses @log_admin_action decorator for event tracking
            - Only updates provided fields
            - Handles password hashing for AdminUser model
            - Supports automatic updated_at timestamp
        """

        @log_admin_action(EventType.UPDATE, model=self.model)
        async def form_update_endpoint_inner(
            request: Request,
            db: AsyncSession = Depends(self.session),
            admin_db: AsyncSession = Depends(self.db_config.get_admin_db),
            current_user: dict = Depends(
                cast(Any, self.admin_site).admin_authentication.get_current_user()
            ),
            event_integration=Depends(lambda: self.event_integration),
            id: Optional[Union[int, str]] = None,
        ) -> Response:
            """Handle POST form submission to update an existing record."""
            assert self.admin_site is not None

            if id is None:
                return JSONResponse(
                    status_code=422, content={"message": "No id parameter provided"}
                )

            converted_id = self._convert_id_to_pk_type(id)

            item = await self.crud.get(
                db=db, id=converted_id, schema_to_select=self.select_schema
            )
            if not item:
                return JSONResponse(
                    status_code=404, content={"message": f"Item with id {id} not found"}
                )

            form_fields = _get_form_fields_from_schema(self.update_schema)
            error_message: Optional[str] = None
            field_errors: Dict[str, str] = {}
            field_values: Dict[str, Any] = {}

            try:
                form_data = await request.form()
                update_data: Dict[str, Any] = {}
                has_updates = False

                for key, raw_val in form_data.items():
                    if isinstance(raw_val, UploadFile):
                        field_values[key] = raw_val
                        update_data[key] = raw_val
                        has_updates = True
                    elif isinstance(raw_val, str):
                        val_str = raw_val.strip()
                        if val_str:
                            update_data[key] = val_str
                            field_values[key] = val_str
                            has_updates = True

                if not has_updates:
                    error_message = "No changes were provided for update"
                else:
                    if self.update_internal_schema is not None and hasattr(
                        self.update_internal_schema, "__fields__"
                    ):
                        fields_dict = cast(
                            Dict[str, Any], self.update_internal_schema.__fields__
                        )
                        if "updated_at" in fields_dict:
                            update_data["updated_at"] = dt.now(datetime.UTC)

                    try:
                        if self.password_transformer is not None:
                            update_schema_instance = self.update_schema(**update_data)

                            transformed_data = (
                                self.password_transformer.transform_update_data(
                                    update_data, update_schema_instance
                                )
                            )

                            if self.model.__name__ == "AdminUser":
                                from ..admin_user.schemas import AdminUserUpdateInternal

                                admin_update_schema: AdminUserUpdateInternal = (
                                    AdminUserUpdateInternal(**transformed_data)
                                )
                                await self.crud.update(
                                    db=db, id=converted_id, object=admin_update_schema
                                )
                            else:
                                if self.update_internal_schema:
                                    generic_update_schema = self.update_internal_schema(
                                        **transformed_data
                                    )
                                    await self.crud.update(
                                        db=db,
                                        id=converted_id,
                                        object=generic_update_schema,
                                    )
                                else:
                                    dynamic_update_schema = type(
                                        "InternalSchema", (BaseModel,), {}
                                    )(**transformed_data)
                                    await self.crud.update(
                                        db=db,
                                        id=converted_id,
                                        object=dynamic_update_schema,
                                    )

                            await db.commit()
                        else:
                            update_schema_instance = self.update_schema(**update_data)
                            await self.crud.update(
                                db=db, id=converted_id, object=update_schema_instance
                            )
                            await db.commit()

                        model_list_url = (
                            f"{self.get_url_prefix()}/{self.model.__name__}/"
                        )
                        return RedirectResponse(
                            url=model_list_url,
                            status_code=303,
                        )

                    except ValidationError as e:
                        field_errors = {
                            str(err["loc"][0]): err["msg"] for err in e.errors()
                        }
                        error_message = "Please correct the errors below."
                    except Exception as e:
                        error_message = str(e)

            except Exception as e:
                error_message = str(e)

            for field in form_fields:
                field_name = field["name"]
                if field_name not in field_values and field_name in item:
                    field_values[field_name] = item[field_name]

            context: Dict[str, Any] = {
                "request": request,
                "model_name": self.model_key,
                "form_fields": form_fields,
                "error": error_message,
                "field_errors": field_errors,
                "field_values": field_values,
                "url_prefix": self.get_url_prefix(),
                "id": id,
                "include_sidebar_and_header": False,
            }

            return self.templates.TemplateResponse(
                "admin/model/update.html",
                context,
                status_code=400 if error_message else 200,
            )

        return cast(EndpointCallable, form_update_endpoint_inner)

    def table_body_content(self) -> EndpointCallable:
        """
        Create endpoint for HTMX-powered table content updates.

        Returns:
            FastAPI route handler for table content partial

        Query Parameters:
            - page: Page number (default: 1)
            - rows-per-page-select: Records per page (default: 10)
            - column-to-search: Column to search in
            - search: Search term

        Example:
            ```python
            # HTMX request for filtered content
            response = await client.get(
                "/table-content?page=2&column-to-search=name&search=test",
                headers={"HX-Request": "true"}
            )
            ```
        """

        async def table_body_content_inner(
            request: Request,
            db: AsyncSession = Depends(self.session),
        ) -> Response:
            """Return HTMX partial for table content with pagination/search."""
            page_str = request.query_params.get("page", "1")
            limit_str = request.query_params.get("rows-per-page-select", "10")

            try:
                page = int(page_str)
                limit = int(limit_str)
            except ValueError:
                page = 1
                limit = 10

            offset = (page - 1) * limit
            search_column = request.query_params.get("column-to-search")
            search_value = request.query_params.get("search", "")

            filter_criteria: Dict[str, Any] = {}
            if search_column and search_value:
                filter_criteria[f"{search_column}__ilike"] = f"%{search_value}%"

            items_result = await self.crud.get_multi(
                db=db,
                offset=offset,
                limit=limit,
                schema_to_select=self.select_schema,
                **cast(Any, filter_criteria),
            )

            items: Dict[str, Any] = {
                "data": items_result.get("data", []),
                "total_count": items_result.get("total_count", 0),
            }

            total_items = items["total_count"]
            total_pages = (total_items + limit - 1) // limit

            return self.templates.TemplateResponse(
                "model/components/table_content.html",
                {
                    "request": request,
                    "model_items": items["data"],
                    "current_page": page,
                    "rows_per_page": limit,
                    "total_items": total_items,
                    "total_pages": total_pages,
                },
            )

        return cast(EndpointCallable, table_body_content_inner)
