"""One real check against precleared.dev, run only when PRECLEARED_LIVE=1 (the CI live job).

It checks the address on Precleared's own website and fetches the receipt, so
it spends one check from the free allowance of the network it runs on.
"""

import os

import pytest

from conftest import client

pytestmark = pytest.mark.skipif(os.environ.get("PRECLEARED_LIVE") != "1", reason="set PRECLEARED_LIVE=1 to call the real API")


def test_live_check_and_receipt():
    body, problem = client.build_check_body(
        "hello@precleared.dev", "GB", address_source="published", pitch="compliance checks for cold email"
    )
    assert problem is None
    result = client.check(body, None, timeout=30)
    if result.status == 429:
        pytest.skip("the free allowance for this network is used up today")
    assert result.status == 200, client.error_message(result)
    assert client.verdict_of(result.body) in client.VERDICTS
    receipt_id = client.receipt_id_of(result.body)
    assert client.normalise_receipt_id(receipt_id) == receipt_id
    receipt = client.get_receipt(receipt_id, None, timeout=30)
    assert receipt.status == 200, client.error_message(receipt)
    assert receipt.body["id"] == receipt_id
