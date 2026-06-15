from django.test import SimpleTestCase

from apps.operations.normalize import normalize_product_operations


class NormalizeProductOperationsTests(SimpleTestCase):
    def test_drops_duplicate_dock(self):
        ops = [
            {"id": "a", "product_id": "p1", "op_name": "DOCK", "op_number": 0},
            {"id": "b", "product_id": "p1", "op_name": "DOCK", "op_number": 170},
            {"id": "c", "product_id": "p1", "op_name": "PRINT", "op_number": 10},
        ]
        out = normalize_product_operations(ops)
        self.assertEqual([o["id"] for o in out], ["a", "c"])
        self.assertEqual(out[0]["op_number"], 0)

    def test_renumbers_single_dock_to_zero(self):
        ops = [{"id": "a", "product_id": "p1", "op_name": "DOCK", "op_number": 10}]
        out = normalize_product_operations(ops)
        self.assertEqual(out[0]["op_number"], 0)
