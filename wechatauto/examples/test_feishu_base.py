"""飞书多维表格初始化逻辑的无网络测试。"""

from __future__ import annotations

import unittest

from wechatauto.feishu_base import FeishuBaseClient, parse_base_url


class _Item:
    def __init__(self, **values):
        self.__dict__.update(values)


class _FakeClient(FeishuBaseClient):
    def __init__(self):
        self.tables = []
        self.fields = {}
        self.created_records = []
        self.deleted_records = []

    def list_tables(self):
        return [_Item(name=name, table_id=table_id) for name, table_id in self.tables]

    def create_table(self, name, fields):
        table_id = "tbl_%d" % (len(self.tables) + 1)
        self.tables.append((name, table_id))
        self.fields[table_id] = [definition[0] for definition in fields]
        return table_id

    def list_fields(self, table_id):
        return [_Item(field_name=name) for name in self.fields.get(table_id, [])]

    def create_field(self, table_id, definition):
        self.fields.setdefault(table_id, []).append(definition[0])

    def create_record(self, table_id, fields, client_token=None):
        record_id = "rec_%d" % (len(self.created_records) + 1)
        self.created_records.append((table_id, fields, client_token, record_id))
        return record_id

    def delete_record(self, table_id, record_id):
        self.deleted_records.append((table_id, record_id))


class FeishuBaseTest(unittest.TestCase):
    def test_parse_base_url(self):
        token, table_id = parse_base_url(
            "https://example.feishu.cn/base/base_token?table=table_id&view=view_id"
        )
        self.assertEqual((token, table_id), ("base_token", "table_id"))

    def test_schema_and_linked_record_smoke_are_idempotent(self):
        client = _FakeClient()
        tables = client.ensure_business_tables()
        self.assertEqual(len(client.tables), 2)
        client.ensure_business_tables()
        self.assertEqual(len(client.tables), 2)
        client.smoke_test(tables)
        project_fields = client.created_records[1][1]
        self.assertEqual(project_fields["关联客户"], [client.created_records[0][3]])
        self.assertEqual(len(client.deleted_records), 2)


if __name__ == "__main__":
    unittest.main()
