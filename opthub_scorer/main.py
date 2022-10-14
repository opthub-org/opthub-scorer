# coding: utf-8
"""
Definition of CLI commands.
"""
import json
import logging
from os import path
from time import sleep
from traceback import format_exc

import click
import docker
import yaml
from click.types import StringParamType
from gql import Client, gql
from gql.transport.requests import RequestsHTTPTransport

_logger = logging.getLogger(__name__)


class AliasedGroup(click.Group):
    """A Click group with short subcommands.

    Example
    -------
    >>> @click.command(cls=AliasedGroup)
    >>> def long_name_command():
    ...     pass
    """

    def get_command(
        self, ctx, cmd_name
    ):  # pylint: disable=inconsistent-return-statements
        cmd = click.Group.get_command(self, ctx, cmd_name)
        if cmd is not None:
            return cmd
        matches = [cmd for cmd in self.list_commands(ctx) if cmd.startswith(cmd_name)]
        if not matches:
            return None
        if len(matches) == 1:
            return click.Group.get_command(self, ctx, matches[0])
        ctx.fail("Too many matches: %s" % ", ".join(sorted(matches)))


class StrLength(StringParamType):
    """A Click option type of string with length validation.

    This is basically the same as `str`, except for additional
    functionalities of length validation.

    :param min: Minimum length
    :param max: Maximum length
    :param clamp: Clamp the input if exeeded
    """

    def __init__(
        self, min=None, max=None, clamp=False
    ):  # pylint: disable=redefined-builtin
        self.min = min
        self.max = max
        self.clamp = clamp

    def convert(self, value, param, ctx):
        ret = StringParamType.convert(self, value, param, ctx)
        len_ret = len(ret)
        if self.clamp:
            if self.min is not None and len_ret < self.min:
                return ret + " " * (self.min - len_ret)
            if self.max is not None and len_ret > self.max:
                return ret[: self.max]
        if (
            self.min is not None
            and len_ret < self.min
            or self.max is not None
            and len_ret > self.max
        ):
            if self.min is None:
                self.fail(
                    "Length %d is longer than the maximum valid length %d."
                    % (len_ret, self.max),
                    param,
                    ctx,
                )
            elif self.max is None:
                self.fail(
                    "Length %d is shorter than the minimum valid length %d."
                    % (len_ret, self.min),
                    param,
                    ctx,
                )
            else:
                self.fail(
                    "Length %d is not in the valid range of %d to %d."
                    % (len_ret, self.min, self.max),
                    param,
                    ctx,
                )
        return ret

    def __repr__(self):
        return "StrLength(%d, %d)" % (self.min, self.max)


def load_config(ctx, self, value):  # pylint: disable=unused-argument
    """Load `ctx.default_map` from a file.

    :param ctx: Click context
    :param self: Self
    :param value: File name
    :return dict: Loaded config
    """

    if not path.exists(value):
        return {}
    with open(value, encoding="utf-8") as file:
        ctx.default_map = yaml.safe_load(file)
    return ctx.default_map


def save_config(ctx, value):
    """Save `ctx.default_map` to a file.

    :param ctx: Click context
    :param value: File name
    :return dict: Saveed config
    """

    with open(value, "w", encoding="utf-8") as file:
        yaml.dump(ctx.default_map, file)
    return ctx.default_map


def query(ctx, gql_doc, **kwargs):
    """Submit a GraphQL query to a database.

    :param ctx: Click context
    :param gql_doc: str: GraphQL query submitted to a database. gql_doc takes either of
    Q_SOLUTION_TO_SCORE, Q_SOLUTIONS_SCORED, Q_START_SCORING, Q_FINISH_SCORING,
    and Q_CANCEL_SCORING.
    :param kwargs: GraphQL variables
    :return response: Results returned from a query (gql_doc). response depends on gql_doc.
    """
    _logger.info("query(%s, %s)", gql_doc, kwargs)
    try:
        response = ctx.obj["client"].execute(gql(gql_doc), variable_values=kwargs)
    except Exception as exc:
        ctx.fail("Exception %s raised when executing query %s\n" % (exc, gql_doc))
    _logger.info("query -> %s", response)
    return response


def wait_to_fetch(ctx, interval):
    """Check if an unscored solution exists in a database by calling query every "interval" seconds.

    :param ctx: Click context
    :param interval: int: Interval to access a database (second)
    :return solution_id: ID of a solution that has not been scored.
    """
    while True:
        response = query(ctx, Q_SOLUTION_TO_SCORE)  # Polling
        if response["solutions"]:
            break  # solution found
        sleep(interval)
    return response["solutions"][0]["id"]


Q_SOLUTION_TO_SCORE = """
query solution_to_score {
  solutions(
    limit: 1
    order_by: { id: asc }
    where: {
      evaluation_finished_at: { _is_null: false }
      evaluation_error: { _is_null: true }
      scoring_started_at: { _is_null: true }
    }
  ) {
    id
  }
}
"""

Q_SOLUTIONS_SCORED = """
query solutions_scored(
  $id: Int!
  $owner_id: String!
  $match_id: Int!
) {
  solutions(
    order_by: { id: asc }
    where: {
      id: { _lt: $id }
      owner_id: { _eq: $owner_id }
      match_id: { _eq: $match_id }
      scoring_finished_at: { _is_null: false }
      evaluation_error: { _is_null: true }
      scoring_error: { _is_null: true }
    }
  ) {
    id
    objective
    constraint
    score
  }
}
"""

Q_START_SCORING = """
mutation start_scoring(
  $id: Int!
) {
  update_solutions(
    where: {
      id: { _eq: $id }
      scoring_started_at: { _is_null: true }
    }
    _set: {
      scoring_started_at: "now()"
    }
  ) {
    affected_rows
    returning {
      id
      owner_id
      match_id
      match {
        indicator { image }
        environments {
          key
          value
        }
      }
      objective
      constraint
    }
  }
}
"""

Q_FINISH_SCORING = """
mutation finish_scoring(
  $id: Int!
  $score: jsonb
  $error: String
) {
  update_solutions_by_pk(
    pk_columns: { id: $id }
    _set: {
      score: $score
      scoring_error: $error
      scoring_finished_at: "now()"
    }) {
    id
    updated_at
  }
}
"""

Q_CANCEL_SCORING = """
mutation cancel_scoring(
  $id: Int!
) {
  update_solutions_by_pk(
    pk_columns: { id: $id }
    _set: {
      score: null
      scoring_started_at: null
      scoring_finished_at: null
    }) {
    id
    updated_at
  }
}
"""


@click.command(help="OptHub Scorer.")
@click.option(
    "-u",
    "--url",
    envvar="OPTHUB_URL",
    type=str,
    default="https://opthub-api.herokuapp.com/v1/graphql",
    help="URL to OptHub.",
)
@click.option(
    "-a", "--apikey", envvar="OPTHUB_APIKEY", type=StrLength(max=64), help="ApiKey."
)
@click.option(
    "-i",
    "--interval",
    envvar="OPTHUB_INTERVAL",
    type=click.IntRange(min=1),
    default=2,
    help="Polling interval.",
)
@click.option(
    "--verify/--no-verify",
    envvar="OPTHUB_VERIFY",
    default=True,
    help="Verify SSL certificate.",
)
@click.option(
    "-r",
    "--retries",
    envvar="OPTHUB_RETRIES",
    type=click.IntRange(min=0),
    default=3,
    help="Retries to establish HTTPS connection.",
)
@click.option(
    "-t",
    "--timeout",
    envvar="OPTHUB_TIMEOUT",
    type=click.IntRange(min=0),
    default=600,
    help="Timeout to process a query.",
)
@click.option(
    "--rm", envvar="OPTHUB_REMOVE", is_flag=True, help="Remove containers after exit."
)
@click.option("-q", "--quiet", count=True, help="Be quieter.")
@click.option("-v", "--verbose", count=True, help="Be more verbose.")
@click.option(
    "-c",
    "--config",
    envvar="OPTHUB_SCORER_CONFIG",
    type=click.Path(dir_okay=False),
    default="opthub-scorer.yml",
    is_eager=True,
    callback=load_config,
    help="Configuration file.",
)
@click.version_option()
@click.argument("command", envvar="OPTHUB_COMMAND", type=str, nargs=-1)
@click.pass_context
def run(ctx, **kwargs):
    """The entrypoint of CLI.

    :param ctx: Click context
    :param kwargs: GraphQL variables
    """
    verbosity = 10 * (kwargs["quiet"] - kwargs["verbose"])
    log_level = logging.WARNING + verbosity
    logging.basicConfig(level=log_level)
    _logger.info("Log level is set to %d", log_level)
    _logger.info("run(%s)", kwargs)
    transport = RequestsHTTPTransport(
        url=kwargs["url"],
        verify=kwargs["verify"],
        retries=kwargs["retries"],
        headers={"X-Hasura-Admin-Secret": kwargs["apikey"]},
    )
    ctx.obj = {
        "client": Client(
            transport=transport,
            fetch_schema_from_transport=True,
        )
    }

    _logger.info("Connect to docker daemon...")
    client = docker.from_env()
    _logger.info("...Connected")

    n_solution = 1
    _logger.info("==================== Solution: %d ====================", n_solution)
    while True:
        try:
            _logger.info("Find solution to score...")
            solution_id = wait_to_fetch(ctx, kwargs["interval"])
            _logger.debug(solution_id)
            _logger.info("...Found")
        except Exception as exc:
            if isinstance(exc, InterruptedError):
                _logger.info(exc)
                _logger.info("Attempt graceful shutdown...")
                _logger.info("No need to rollback")
                _logger.info("...Shutted down")
                ctx.exit(0)
            else:
                _logger.error(format_exc())
                continue

        try:
            _logger.info("Try to lock solution...")
            response = query(ctx, Q_START_SCORING, id=solution_id)
            if response["update_solutions"]["affected_rows"] == 0:
                _logger.info("...Already locked")
                continue
            if response["update_solutions"]["affected_rows"] != 1:
                _logger.error(
                    "Lock error: affected_rows must be 0 or 1, but %s", response
                )
            solution = response["update_solutions"]["returning"][0]
            _logger.info("...Lock aquired")

            _logger.info("Parse solution to score...")
            _logger.debug(solution)
            sol = json.dumps(solution) + "\n"
            _logger.debug(sol)
            _logger.info("...Parsed")

            _logger.info("Pull solutions scored")
            response = query(
                ctx,
                Q_SOLUTIONS_SCORED,
                id=solution_id,
                owner_id=solution["owner_id"],
                match_id=solution["match_id"],
            )
            _logger.debug(response)
            _logger.info("...Pulled")

            _logger.info("Parse solutions scored...")
            _logger.debug(response["solutions"])
            sols = json.dumps(response["solutions"]) + "\n"
            _logger.debug(sols)
            _logger.info("...Parsed")

            _logger.info("Start container...")
            _logger.debug(solution["match"]["indicator"]["image"])
            container = client.containers.run(
                image=solution["match"]["indicator"]["image"],
                command=kwargs["command"],
                environment={
                    v["key"]: v["value"] for v in solution["match"]["environments"]
                },
                stdin_open=True,
                detach=True,
            )
            _logger.info("...Started: %s", container.name)

            _logger.info("Send solutions...")
            socket = container.attach_socket(
                params={"stdin": 1, "stream": 1, "stdout": 1, "stderr": 1}
            )
            socket._sock.sendall(
                (sol + sols).encode("utf-8")
            )  # pylint: disable=protected-access
            _logger.info("...Send")

            _logger.info("Wait for scoring...")
            container.wait(timeout=kwargs["timeout"])
            _logger.info("...Scored")

            _logger.info("Recieve stdout...")
            stdout = container.logs(stdout=True, stderr=False).decode("utf-8")
            _logger.debug(stdout)
            _logger.info("...Recived")

            if kwargs["rm"]:
                _logger.info("Remove container...")
                container.remove()
                _logger.info("...Removed")

            _logger.info("Parse stdout...")
            stdout = json.loads(stdout)
            _logger.debug(stdout)
            _logger.info("...Parsed")

            _logger.info("Push scoring...")
            query(
                ctx,
                Q_FINISH_SCORING,
                id=solution["id"],
                score=stdout.get("score"),
                error=stdout.get("error"),
            )
            _logger.info("...Pushed")
        except Exception as exc:
            if isinstance(exc, InterruptedError):
                _logger.info(exc)
                _logger.info("Attempt graceful shutdown...")
                _logger.info("Rollback scoring...")
                query(ctx, Q_CANCEL_SCORING, id=solution["id"])
                _logger.info("...Rolled back")
                _logger.info("...Shutted down")
                ctx.exit(0)
            _logger.error(format_exc())
            _logger.info("Finish scoring...")
            query(
                ctx,
                Q_FINISH_SCORING,
                id=solution["id"],
                score=None,
                error=str(exc),
            )
            _logger.info("...Finished")
            continue

        n_solution += 1
        _logger.info(
            "==================== Solution: %d ====================", n_solution
        )
