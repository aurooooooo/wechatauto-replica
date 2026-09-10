# -*- coding: utf-8 -*-
"""飞书多维表格的最小客户端与客户/项目表初始化。"""

from __future__ import annotations

import os
from urllib.parse import parse_qs, urlparse


CUSTOMER_FIELDS = [
    ("客户编号", 1, None),
    ("客户姓名", 1, None),
    ("联系电话", 13, None),
    ("公司", 1, None),
    ("微信ID", 1, None),
    ("客户偏好", 1, None),
    ("来源会话", 1, None),
    ("确认状态", 3, None),
    ("更新时间", 5, {"date_formatter": "yyyy-MM-dd HH:mm"}),
]

PROJECT_FIELDS = [
    ("项目编号", 1, None),
    ("项目名称", 1, None),
    ("当前阶段", 1, None),
    ("进展摘要", 1, None),
    ("关联群聊", 1, None),
    ("确认状态", 3, None),
    ("更新时间", 5, {"date_formatter": "yyyy-MM-dd HH:mm"}),
]


def parse_base_url(url: str) -> tuple[str, str | None]:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[-2] != "base":
        raise ValueError("不是有效的飞书多维表格地址")
    return parts[-1], (parse_qs(parsed.query).get("table") or [None])[0]


class FeishuBaseClient:
    def __init__(self, app_id: str, app_secret: str, app_token: str):
        if not app_id or not app_secret or not app_token:
            raise ValueError("FEISHU_APP_ID、FEISHU_APP_SECRET、FEISHU_BASE_APP_TOKEN 不能为空")
        try:
            import lark_oapi as lark
            import lark_oapi.api.bitable.v1 as bitable
        except ImportError as exc:
            raise RuntimeError('缺少飞书依赖，请执行：pip install -e ".[feishu]"') from exc
        self.app_token = app_token
        self._b = bitable
        self._client = (
            lark.Client.builder().app_id(app_id).app_secret(app_secret)
            .log_level(lark.LogLevel.WARNING).build()
        )

    @classmethod
    def from_env(cls) -> "FeishuBaseClient":
        return cls(
            os.environ.get("FEISHU_APP_ID", ""),
            os.environ.get("FEISHU_APP_SECRET", ""),
            os.environ.get("FEISHU_BASE_APP_TOKEN", ""),
        )

    @staticmethod
    def _ok(response, action: str):
        if not response.success():
            raise RuntimeError("飞书%s失败：code=%s, msg=%s, log_id=%s" % (
                action, response.code, response.msg, response.get_log_id(),
            ))
        return response.data

    def list_tables(self) -> list:
        request = (
            self._b.ListAppTableRequest.builder().app_token(self.app_token)
            .page_size(100).build()
        )
        data = self._ok(self._client.bitable.v1.app_table.list(request), "读取数据表")
        return list(data.items or [])

    def list_fields(self, table_id: str) -> list:
        request = (
            self._b.ListAppTableFieldRequest.builder().app_token(self.app_token)
            .table_id(table_id).page_size(100).build()
        )
        data = self._ok(
            self._client.bitable.v1.app_table_field.list(request), "读取字段",
        )
        return list(data.items or [])

    def create_table(self, name: str, fields: list[tuple]) -> str:
        headers = [self._header(field) for field in fields]
        table = (
            self._b.ReqTable.builder().name(name).default_view_name("全部")
            .fields(headers).build()
        )
        body = self._b.CreateAppTableRequestBody.builder().table(table).build()
        request = (
            self._b.CreateAppTableRequest.builder().app_token(self.app_token)
            .request_body(body).build()
        )
        data = self._ok(self._client.bitable.v1.app_table.create(request), "创建数据表")
        return data.table_id

    def _property(self, values: dict | None):
        if not values:
            return None
        builder = self._b.AppTableFieldProperty.builder()
        for key, value in values.items():
            getattr(builder, key)(value)
        return builder.build()

    def _header(self, definition: tuple):
        name, field_type, properties = definition
        builder = self._b.AppTableCreateHeader.builder().field_name(name).type(field_type)
        prop = self._property(properties)
        return builder.property(prop).build() if prop else builder.build()

    def create_field(self, table_id: str, definition: tuple) -> None:
        name, field_type, properties = definition
        builder = self._b.AppTableField.builder().field_name(name).type(field_type)
        prop = self._property(properties)
        field = builder.property(prop).build() if prop else builder.build()
        request = (
            self._b.CreateAppTableFieldRequest.builder().app_token(self.app_token)
            .table_id(table_id).request_body(field).build()
        )
        self._ok(self._client.bitable.v1.app_table_field.create(request), "创建字段")

    def ensure_fields(self, table_id: str, definitions: list[tuple]) -> None:
        existing = {field.field_name for field in self.list_fields(table_id)}
        for definition in definitions:
            if definition[0] not in existing:
                self.create_field(table_id, definition)

    def ensure_business_tables(self) -> dict[str, str]:
        tables = {table.name: table.table_id for table in self.list_tables()}
        customer_id = tables.get("客户信息") or self.create_table("客户信息", CUSTOMER_FIELDS)
        self.ensure_fields(customer_id, CUSTOMER_FIELDS)
        project_definitions = PROJECT_FIELDS + [
            ("关联客户", 18, {"table_id": customer_id, "multiple": True}),
        ]
        project_id = tables.get("项目管理") or self.create_table(
            "项目管理", project_definitions,
        )
        self.ensure_fields(project_id, project_definitions)
        return {"customer_table_id": customer_id, "project_table_id": project_id}

    def create_record(self, table_id: str, fields: dict, client_token: str | None = None) -> str:
        body = self._b.AppTableRecord.builder().fields(fields).build()
        builder = (
            self._b.CreateAppTableRecordRequest.builder().app_token(self.app_token)
            .table_id(table_id).request_body(body)
        )
        if client_token:
            builder.client_token(client_token)
        data = self._ok(
            self._client.bitable.v1.app_table_record.create(builder.build()), "创建记录",
        )
        return data.record.record_id

    def delete_record(self, table_id: str, record_id: str) -> None:
        request = (
            self._b.DeleteAppTableRecordRequest.builder().app_token(self.app_token)
            .table_id(table_id).record_id(record_id).build()
        )
        self._ok(self._client.bitable.v1.app_table_record.delete(request), "删除记录")

    def smoke_test(self, tables: dict[str, str]) -> None:
        customer_record = project_record = None
        try:
            customer_record = self.create_record(tables["customer_table_id"], {
                "客户编号": "TEST-CUSTOMER", "客户姓名": "联调测试客户",
                "确认状态": "测试",
            })
            project_record = self.create_record(tables["project_table_id"], {
                "项目编号": "TEST-PROJECT", "项目名称": "联调测试项目",
                "关联客户": [customer_record], "确认状态": "测试",
            })
        finally:
            if project_record:
                self.delete_record(tables["project_table_id"], project_record)
            if customer_record:
                self.delete_record(tables["customer_table_id"], customer_record)
