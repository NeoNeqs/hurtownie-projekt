from __future__ import annotations

from warehouse.nvd import parse_metric


def test_parse_metric_prefers_cvss_v31() -> None:
    payload = {
        "vulnerabilities": [
            {
                "cve": {
                    "metrics": {
                        "cvssMetricV31": [
                            {
                                "cvssData": {
                                    "baseScore": 7.5,
                                    "baseSeverity": "HIGH",
                                    "vectorString": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H",
                                }
                            }
                        ],
                        "cvssMetricV2": [
                            {
                                "cvssData": {
                                    "baseScore": 5.0,
                                    "vectorString": "AV:N/AC:L/Au:N/C:N/I:N/A:P",
                                },
                                "baseSeverity": "MEDIUM",
                            }
                        ],
                    },
                    "published": "2024-01-01T00:00:00.000",
                    "lastModified": "2024-02-01T00:00:00.000",
                }
            }
        ]
    }

    metric = parse_metric(payload)

    assert metric is not None
    assert metric["severity"] == "HIGH"
    assert metric["cvss_score"] == 7.5
    assert metric["cvss_vector"].startswith("CVSS:3.1/")


def test_parse_metric_falls_back_to_v2_severity() -> None:
    payload = {
        "vulnerabilities": [
            {
                "cve": {
                    "metrics": {
                        "cvssMetricV2": [
                            {
                                "cvssData": {
                                    "baseScore": 4.3,
                                    "vectorString": "AV:N/AC:M/Au:N/C:N/I:P/A:N",
                                },
                                "baseSeverity": "MEDIUM",
                            }
                        ]
                    }
                }
            }
        ]
    }

    metric = parse_metric(payload)

    assert metric is not None
    assert metric["severity"] == "MEDIUM"
    assert metric["cvss_score"] == 4.3


def test_parse_metric_returns_none_without_metrics() -> None:
    assert parse_metric({"vulnerabilities": []}) is None
    assert parse_metric({"vulnerabilities": [{"cve": {"metrics": {}}}]}) is None
