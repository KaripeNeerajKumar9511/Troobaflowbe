import json
from unittest import mock

from django.test import RequestFactory, SimpleTestCase

from apps.simulations.dll_full_calculate import (
    canonicalize_routing_for_dll,
    DllRunDiagnostics,
    _build_dll_model_payload,
    normalize_operations_for_dll,
    normalize_routing_against_operations_for_dll,
    normalize_routing_rows_for_dll,
    repair_routing_for_dll,
    run_full_calculate_via_dll,
)
from apps.simulations.latest_views import full_calculate_view


SAMPLE_MODEL = {
    "general": {"util_limit": 95.0},
    "labor": [{"id": "labor-1", "name": "Labor A"}],
    "equipment": [{"id": "eq-1", "name": "Eq A"}],
    "products": [{"id": "prod-1", "name": "Prod A", "demand": 100}],
    "operations": [
        {"product_id": "prod-1", "op_name": "DOCK", "op_number": 0, "equip_id": "eq-1"},
        {"product_id": "prod-1", "op_name": "STOCK", "op_number": 10000, "equip_id": "eq-1"},
    ],
    "routing": [{"product_id": "prod-1", "from_op_name": "DOCK", "to_op_name": "STOCK", "pct_routed": 100}],
    "ibom": [],
}


class FullCalculateViewTests(SimpleTestCase):
    def setUp(self) -> None:
        self.factory = RequestFactory()

    @mock.patch("apps.simulations.latest_views.run_full_calculate_via_dll")
    def test_full_calculate_returns_results_from_dll_service(self, run_dll_mock):
        run_dll_mock.return_value = {"equipment": [], "labor": [], "products": [], "operations": []}
        request = self.factory.post(
            "/api/simulations/full-calculate",
            data=json.dumps({"model": SAMPLE_MODEL}),
            content_type="application/json",
        )

        response = full_calculate_view(request)
        body = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertIn("results", body)
        run_dll_mock.assert_called_once()

    @mock.patch("apps.simulations.latest_views.run_full_calculate_via_dll")
    def test_full_calculate_returns_diagnostics_on_dll_failure(self, run_dll_mock):
        run_dll_mock.side_effect = DllRunDiagnostics(
            "DLL run failed",
            {"stage": "run_model_from_json", "errorType": "RuntimeError"},
        )
        request = self.factory.post(
            "/api/simulations/full-calculate",
            data=json.dumps({"model": SAMPLE_MODEL}),
            content_type="application/json",
        )

        response = full_calculate_view(request)
        body = json.loads(response.content)

        self.assertEqual(response.status_code, 422)
        self.assertEqual(body["errorType"], "DllRunDiagnostics")
        self.assertEqual(body["diagnostics"]["stage"], "run_model_from_json")

    @mock.patch("apps.simulations.latest_views.full_calculate_corrected")
    @mock.patch("apps.simulations.latest_views.run_full_calculate_via_dll")
    def test_full_calculate_falls_back_to_python_on_routing_21b(self, run_dll_mock, fallback_mock):
        run_dll_mock.side_effect = DllRunDiagnostics(
            "DLL returned non-zero status code: -1 (INTERNAL ERROR: #(21b) - Routing cannot be solved for product)",
            {"stage": "dll_return_code"},
        )
        fallback_mock.return_value = {"equipment": [], "labor": [], "products": [], "operations": [], "warnings": [], "errors": []}
        request = self.factory.post(
            "/api/simulations/full-calculate",
            data=json.dumps({"model": SAMPLE_MODEL}),
            content_type="application/json",
        )

        response = full_calculate_view(request)
        body = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(body.get("fallbackUsed"), "python")
        self.assertIn("results", body)
        self.assertTrue(any("DLL routing solver failed" in w for w in body["results"]["warnings"]))
        self.assertEqual(body["results"]["errors"], [])
        fallback_mock.assert_called_once()


class RoutingRepairTests(SimpleTestCase):
    def test_injects_dock_when_missing(self):
        pid = "prod-bolt"
        products = [{"id": pid, "name": "BOLT", "demand": 0}]
        operations = [
            {"product_id": pid, "op_name": "DOCK", "op_number": 0, "equip_id": ""},
            {"product_id": pid, "op_name": "UNPACK", "op_number": 10, "equip_id": "eq1"},
            {"product_id": pid, "op_name": "INSPECT", "op_number": 20, "equip_id": "eq2"},
            {"product_id": pid, "op_name": "STOCK", "op_number": 30, "equip_id": ""},
        ]
        routing = [
            {"product_id": pid, "from_op_name": "UNPACK", "to_op_name": "INSPECT", "pct_routed": 100},
            {"product_id": pid, "from_op_name": "INSPECT", "to_op_name": "STOCK", "pct_routed": 100},
        ]
        out = repair_routing_for_dll(products, operations, routing)
        dock_edges = [r for r in out if r["from_op_name"].upper() == "DOCK"]
        self.assertEqual(len(dock_edges), 1)
        self.assertEqual(dock_edges[0]["to_op_name"], "UNPACK")
        self.assertEqual(dock_edges[0]["pct_routed"], 100.0)

    def test_fills_inspect_branch_remainder_to_scrap(self):
        pid = "prod-hub"
        products = [{"id": pid, "name": "HUB", "demand": 1}]
        operations = [
            {"product_id": pid, "op_name": "DOCK", "op_number": 0, "equip_id": ""},
            {"product_id": pid, "op_name": "INSPECT", "op_number": 50, "equip_id": "eq1"},
            {"product_id": pid, "op_name": "REWORK", "op_number": 60, "equip_id": "eq2"},
            {"product_id": pid, "op_name": "SLOT", "op_number": 70, "equip_id": "eq2"},
            {"product_id": pid, "op_name": "STOCK", "op_number": 80, "equip_id": ""},
            {"product_id": pid, "op_name": "SCRAP", "op_number": 90, "equip_id": ""},
        ]
        routing = [
            {"product_id": pid, "from_op_name": "DOCK", "to_op_name": "INSPECT", "pct_routed": 100},
            {"product_id": pid, "from_op_name": "INSPECT", "to_op_name": "SLOT", "pct_routed": 85},
            {"product_id": pid, "from_op_name": "INSPECT", "to_op_name": "REWORK", "pct_routed": 10},
        ]
        out = repair_routing_for_dll(products, operations, routing)
        inspect = [r for r in out if str(r.get("from_op_name", "")).upper() == "INSPECT"]
        total = sum(float(r["pct_routed"]) for r in inspect)
        self.assertAlmostEqual(total, 100.0, places=2)
        scrap_extra = [r for r in inspect if str(r.get("to_op_name", "")).upper() == "SCRAP"]
        self.assertTrue(any(float(r["pct_routed"]) > 4 for r in scrap_extra))

    def test_adds_stock_path_for_dead_end_operation(self):
        pid = "prod-dead-end"
        products = [{"id": pid, "name": "P", "demand": 1}]
        operations = [
            {"product_id": pid, "op_name": "DOCK", "op_number": 0, "equip_id": ""},
            {"product_id": pid, "op_name": "CUT", "op_number": 10, "equip_id": "eq-1"},
            {"product_id": pid, "op_name": "STOCK", "op_number": 10000, "equip_id": ""},
            {"product_id": pid, "op_name": "SCRAP", "op_number": 10001, "equip_id": ""},
        ]
        routing = [{"product_id": pid, "from_op_name": "DOCK", "to_op_name": "CUT", "pct_routed": 100}]

        out = repair_routing_for_dll(products, operations, routing)
        dead_end = [
            r
            for r in out
            if str(r.get("product_id")) == pid
            and str(r.get("from_op_name", "")).upper() == "CUT"
            and str(r.get("to_op_name", "")).upper() == "STOCK"
        ]
        self.assertEqual(len(dead_end), 1)
        self.assertEqual(float(dead_end[0]["pct_routed"]), 100.0)


class RoutingNormalizeTests(SimpleTestCase):
    def test_flat_snake_case_unchanged(self):
        raw = [{"product_id": "prod-1", "from_op_name": "DOCK", "to_op_name": "STOCK", "pct_routed": 100.0}]
        self.assertEqual(normalize_routing_rows_for_dll(raw), raw)

    def test_nested_entries_shape(self):
        raw = [
            {
                "product_id": "prod-1",
                "entries": [
                    {"from_op_name": "DOCK", "to_op_name": "BENCH", "pct_routed": 100},
                    {"fromOpName": "BENCH", "toOpName": "STOCK", "pctRouted": 100},
                ],
            }
        ]
        out = normalize_routing_rows_for_dll(raw)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["product_id"], "prod-1")
        self.assertEqual(out[1]["to_op_name"], "STOCK")

    def test_canonicalize_routing_sorts_and_merges(self):
        products = [{"id": "prod-1"}, {"id": "prod-2"}]
        operations = [
            {"product_id": "prod-1", "op_name": "DOCK", "op_number": 0},
            {"product_id": "prod-1", "op_name": "STOCK", "op_number": 10000},
            {"product_id": "prod-2", "op_name": "DOCK", "op_number": 0},
            {"product_id": "prod-2", "op_name": "STOCK", "op_number": 10000},
        ]
        routing = [
            {"product_id": "prod-2", "from_op_name": "DOCK", "to_op_name": "STOCK", "pct_routed": 50},
            {"product_id": "prod-1", "from_op_name": "DOCK", "to_op_name": "STOCK", "pct_routed": 100},
            {"product_id": "prod-2", "from_op_name": "DOCK", "to_op_name": "STOCK", "pct_routed": 50},
        ]
        out = canonicalize_routing_for_dll(products, operations, routing)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["product_id"], "prod-1")
        self.assertEqual(out[1]["product_id"], "prod-2")
        self.assertEqual(out[1]["pct_routed"], 100.0)


class OperationsNormalizeTests(SimpleTestCase):
    def test_adds_meta_ops_with_test4_numbers(self):
        products = [{"id": "prod-1"}]
        equipment = [{"id": "equip-10"}]
        ops = [{"product_id": "prod-1", "op_name": "bench", "op_number": 10, "equip_id": "equip-10"}]
        out = normalize_operations_for_dll(products, ops, equipment)
        by_name = {o["op_name"]: o for o in out}
        self.assertEqual(by_name["DOCK"]["op_number"], 0)
        self.assertEqual(by_name["STOCK"]["op_number"], 10000)
        self.assertEqual(by_name["SCRAP"]["op_number"], 10001)
        self.assertIn("BENCH", by_name)

    def test_routing_is_filtered_to_known_ops(self):
        products = [{"id": "prod-1"}]
        equipment = [{"id": "equip-10"}]
        ops = normalize_operations_for_dll(
            products,
            [{"product_id": "prod-1", "op_name": "UNPACK", "op_number": 10, "equip_id": "equip-10"}],
            equipment,
        )
        routes = [
            {"product_id": "prod-1", "from_op_name": "DOCK", "to_op_name": "UNPACK", "pct_routed": 100},
            {"product_id": "prod-1", "from_op_name": "UNPACK", "to_op_name": "MISSING_OP", "pct_routed": 100},
        ]
        out = normalize_routing_against_operations_for_dll(products, ops, routes)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["to_op_name"], "UNPACK")


class DllServiceTests(SimpleTestCase):
    @mock.patch("apps.simulations.dll_full_calculate.run_model_from_json")
    def test_payload_contract_keys_are_written_for_dll_runner(self, run_model_mock):
        run_model_mock.return_value = (0, "json")
        with mock.patch("apps.simulations.dll_full_calculate._parse_dll_outputs", return_value={"equipment": [], "labor": [], "products": [], "operations": [], "warnings": [], "errors": [], "overLimitResources": [], "calculatedAt": "now"}):
            run_full_calculate_via_dll(SAMPLE_MODEL)

        self.assertEqual(run_model_mock.call_count, 1)
        args, kwargs = run_model_mock.call_args
        json_path = args[0]
        self.assertTrue(json_path.endswith(".json"))
        self.assertEqual(kwargs["routing_mode"], "auto")

    @mock.patch("apps.simulations.dll_full_calculate.run_model_from_json")
    @mock.patch("apps.simulations.dll_full_calculate._parse_dll_outputs")
    def test_nonzero_rc_without_fatal_err_still_returns_results(self, parse_outputs_mock, run_model_mock):
        run_model_mock.return_value = (-1, "safe")
        parse_outputs_mock.return_value = {
            "equipment": [],
            "labor": [],
            "products": [],
            "operations": [],
            "warnings": [],
            "errors": [],
            "overLimitResources": [],
            "calculatedAt": "now",
        }

        results = run_full_calculate_via_dll(SAMPLE_MODEL)
        self.assertIn("warnings", results)
        self.assertFalse(
            any("non-zero code" in str(w).lower() for w in results["warnings"]),
            "Benign DLL exit codes should not add confusing warnings when outputs are valid.",
        )

    @mock.patch("apps.simulations.dll_full_calculate.run_model_from_json")
    @mock.patch("apps.simulations.dll_full_calculate._parse_dll_outputs")
    def test_retries_on_dll_exception_and_returns_successful_attempt(self, parse_outputs_mock, run_model_mock):
        run_model_mock.side_effect = [RuntimeError("transient"), (0, "auto")]
        parse_outputs_mock.return_value = {
            "equipment": [],
            "labor": [],
            "products": [],
            "operations": [],
            "warnings": [],
            "errors": [],
            "overLimitResources": [],
            "calculatedAt": "now",
        }

        results = run_full_calculate_via_dll(SAMPLE_MODEL)
        self.assertEqual(run_model_mock.call_count, 2)
        self.assertFalse(any("retry attempt" in str(w).lower() for w in results.get("warnings", [])))

    @mock.patch("apps.simulations.dll_full_calculate.run_model_from_json")
    @mock.patch("apps.simulations.dll_full_calculate._parse_dll_outputs")
    def test_retries_on_fatal_error_then_succeeds(self, parse_outputs_mock, run_model_mock):
        run_model_mock.side_effect = [(-1, "auto"), (0, "auto")]
        parse_outputs_mock.side_effect = [
            {
                "equipment": [],
                "labor": [],
                "products": [],
                "operations": [],
                "warnings": [],
                "errors": ["0,1,INTERNAL ERROR: fatal"],
                "overLimitResources": [],
                "calculatedAt": "now",
            },
            {
                "equipment": [],
                "labor": [],
                "products": [],
                "operations": [],
                "warnings": [],
                "errors": [],
                "overLimitResources": [],
                "calculatedAt": "now",
            },
        ]

        results = run_full_calculate_via_dll(SAMPLE_MODEL)
        self.assertEqual(run_model_mock.call_count, 2)
        self.assertFalse(any("retry attempt" in str(w).lower() for w in results.get("warnings", [])))

    @mock.patch("apps.simulations.dll_full_calculate.run_model_from_json")
    @mock.patch("apps.simulations.dll_full_calculate._parse_dll_outputs")
    def test_retries_on_results_err_rows_even_when_rc_zero(self, parse_outputs_mock, run_model_mock):
        run_model_mock.side_effect = [(0, "auto"), (0, "auto")]
        parse_outputs_mock.side_effect = [
            {
                "equipment": [],
                "labor": [],
                "products": [],
                "operations": [],
                "warnings": [],
                "errors": ["1,7,Transient DLL warning row"],
                "overLimitResources": [],
                "calculatedAt": "now",
            },
            {
                "equipment": [],
                "labor": [],
                "products": [],
                "operations": [],
                "warnings": [],
                "errors": [],
                "overLimitResources": [],
                "calculatedAt": "now",
            },
        ]

        results = run_full_calculate_via_dll(SAMPLE_MODEL)
        self.assertEqual(run_model_mock.call_count, 2)
        self.assertEqual(results.get("errors"), [])

    @mock.patch("apps.simulations.dll_full_calculate.run_model_from_json")
    @mock.patch("apps.simulations.dll_full_calculate._parse_dll_outputs")
    def test_retries_when_over_limit_resources_are_present(self, parse_outputs_mock, run_model_mock):
        run_model_mock.side_effect = [(0, "auto"), (0, "auto")]
        parse_outputs_mock.side_effect = [
            {
                "equipment": [],
                "labor": [],
                "products": [],
                "operations": [],
                "warnings": ['Equipment "Eq A" util (96%) > limit (95%)'],
                "errors": [],
                "overLimitResources": ["Equipment: Eq A (96%)"],
                "calculatedAt": "now",
            },
            {
                "equipment": [],
                "labor": [],
                "products": [],
                "operations": [],
                "warnings": [],
                "errors": [],
                "overLimitResources": [],
                "calculatedAt": "now",
            },
        ]

        results = run_full_calculate_via_dll(SAMPLE_MODEL)
        self.assertEqual(run_model_mock.call_count, 2)
        self.assertEqual(results.get("overLimitResources"), [])

    @mock.patch("apps.simulations.dll_full_calculate.run_model_from_json")
    def test_returns_zero_filled_results_after_all_run_model_exceptions(self, run_model_mock):
        run_model_mock.side_effect = RuntimeError("boom")

        results = run_full_calculate_via_dll(SAMPLE_MODEL)

        self.assertEqual(run_model_mock.call_count, 6)
        self.assertIn("equipment", results)
        self.assertIn("labor", results)
        self.assertIn("products", results)
        self.assertIn("operations", results)
        self.assertTrue(any("boom" in w for w in results.get("warnings", [])))

    @mock.patch("apps.simulations.dll_full_calculate.run_model_from_json")
    @mock.patch("apps.simulations.dll_full_calculate._parse_dll_outputs")
    def test_returns_last_parsed_dll_payload_when_all_attempts_retryable(self, parse_outputs_mock, run_model_mock):
        run_model_mock.return_value = (0, "auto")
        fatal = "0,1,INTERNAL ERROR: routing stuck"
        parse_outputs_mock.return_value = {
            "equipment": [],
            "labor": [],
            "products": [],
            "operations": [],
            "warnings": [],
            "errors": [fatal],
            "overLimitResources": [],
            "calculatedAt": "now",
        }

        results = run_full_calculate_via_dll(SAMPLE_MODEL)

        self.assertEqual(run_model_mock.call_count, 6)
        self.assertEqual(parse_outputs_mock.call_count, 6)
        self.assertEqual(results.get("errors"), [fatal])

    def test_payload_prepends_dummy_labor_and_equipment(self):
        payload = _build_dll_model_payload(SAMPLE_MODEL)
        self.assertGreaterEqual(len(payload["labor"]), 2)
        self.assertGreaterEqual(len(payload["equipment"]), 2)
        self.assertEqual(payload["labor"][0]["id"], "__DUMMY_LABOR__")
        self.assertEqual(payload["equipment"][0]["id"], "__DUMMY_EQUIP__")
        self.assertEqual(payload["labor"][0]["count"], -1)
        self.assertEqual(payload["equipment"][0]["count"], -1)

    def test_payload_ops_order_starts_with_dock_stock_scrap(self):
        payload = _build_dll_model_payload(SAMPLE_MODEL)
        ops = [o for o in payload["operations"] if o.get("product_id") == "prod-1"]
        self.assertGreaterEqual(len(ops), 3)
        self.assertEqual(ops[0]["op_name"], "DOCK")
        self.assertEqual(ops[0]["op_number"], 0)
        self.assertEqual(ops[1]["op_name"], "STOCK")
        self.assertEqual(ops[1]["op_number"], 10000)
        self.assertEqual(ops[2]["op_name"], "SCRAP")
        self.assertEqual(ops[2]["op_number"], 10001)
