# stdlib
import random

# syft absolute
import syft as sy
from syft import test_settings
from syft.service.request.request import RequestStatus

# relative
from ..sim.core import SimulatorContext

__all__ = ["bq_test_query", "bq_submit_query", "bq_check_query_results"]


def query_sql():
    dataset_2 = test_settings.get("dataset_2", default="dataset_2")
    table_2 = test_settings.get("table_2", default="table_2")
    table_2_col_id = test_settings.get("table_2_col_id", default="table_id")
    table_2_col_score = test_settings.get("table_2_col_score", default="colname")

    query = f"SELECT {table_2_col_id}, AVG({table_2_col_score}) AS average_score \
        FROM {dataset_2}.{table_2} \
        GROUP BY {table_2_col_id} \
        LIMIT 10000"
    return query


def bq_test_query(ctx: SimulatorContext, client: sy.DatasiteClient):
    user = client.logged_in_user
    ctx.logger.info(f"User: {user} - Calling client.api.bigquery.test_query (mock)")
    res = client.api.bigquery.test_query(sql_query=query_sql())
    assert len(res) == 10000
    ctx.logger.info(f"User: {user} - Received {len(res)} rows")
    return res


def bq_submit_query(ctx: SimulatorContext, client: sy.DatasiteClient):
    user = client.logged_in_user
    # Randomly define a func_name a function to call
    func_name = "invalid_func" if random.random() < 0.5 else "test_query"

    ctx.logger.info(
        f"User: {user} - Calling client.api.services.bigquery.submit_query func_name={func_name}"
    )
    res = client.api.bigquery.submit_query(
        func_name=func_name,
        query=query_sql(),
    )
    assert isinstance(res, sy.SyftSuccess), res
    ctx.logger.info(f"User: {user} - Received {res}")
    return res


def bq_check_query_results(ctx: SimulatorContext, client: sy.DatasiteClient):
    user = client.logged_in_user

    for request in client.requests:
        status = request.get_status()

        if status == RequestStatus.APPROVED:
            job = request.code(blocking=False)
            result = job.wait()
            assert len(result) == 10000
            ctx.logger.info(
                f"User: {user} - {request.code.service_func_name} - Request approved"
            )
        elif status == RequestStatus.REJECTED:
            ctx.logger.info(
                f"User: {user} - {request.code.service_func_name} - Request rejected"
            )
        else:
            ctx.logger.info(
                f"User: {user} - {request.code.service_func_name} - Request pending"
            )

    return True
