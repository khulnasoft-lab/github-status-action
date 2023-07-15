import argparse
import logging
import os
import json
from datetime import datetime

import sys
from typing import Tuple


import pandas as pd
from github import Github, Repository  # type: ignore
import requests
import retrying  # type: ignore
import pytz


"""
prior art
https://github.com/MTG/github-traffic
https://github.com/nchah/github-traffic-stats/
https://github.com/sangonzal/repository-traffic-action

makes use of code and methods from my other projects at
https://github.com/jgehrcke/dcos-dev-prod-analysis
https://github.com/jgehrcke/bouncer-log-analysis
https://github.com/jgehrcke/goeffel
"""


log = logging.getLogger()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d %(levelname)s:%(threadName)s: %(message)s",
    datefmt="%y%m%d-%H:%M:%S",
)


# Get tz-aware datetime object corresponding to invocation time.
NOW = pytz.timezone("UTC").localize(datetime.utcnow())
INVOCATION_TIME_STRING = NOW.strftime("%Y-%m-%d_%H%M%S")

if not os.environ.get("GHRS_GITHUB_API_TOKEN", None):
    sys.exit("error: environment variable GHRS_GITHUB_API_TOKEN empty or not set")

GHUB = Github(login_or_token=os.environ["GHRS_GITHUB_API_TOKEN"].strip(), per_page=100)


def main() -> None:
    args = parse_args()
    # Full name of repo with slash (including owner/org)
    repo: Repository.Repository = GHUB.get_repo(args.repo)
    log.info("Working with repository `%s`", repo)
    log.info("Request quota limit: %s", GHUB.get_rate_limit())

    (
        df_views_clones,
        df_referrers_snapshot_now,
        df_paths_snapshot_now,
    ) = fetch_all_traffic_api_endpoints(repo)

    outdir_path = args.snapshot_directory
    log.info("current working directory: %s", os.getcwd())
    log.info("write output CSV files to directory: %s", outdir_path)

    if len(df_views_clones):
        df_views_clones.to_csv(
            os.path.join(
                outdir_path,
                f"{INVOCATION_TIME_STRING}_views_clones_series_fragment.csv",
            )
        )
    else:
        log.info("do not write df_views_clones: empty")

    if len(df_referrers_snapshot_now):
        df_referrers_snapshot_now.to_csv(
            os.path.join(
                outdir_path, f"{INVOCATION_TIME_STRING}_top_referrers_snapshot.csv"
            )
        )
    else:
        log.info("do not write df_referrers_snapshot_now: empty")

    if len(df_paths_snapshot_now):
        df_paths_snapshot_now.to_csv(
            os.path.join(
                outdir_path, f"{INVOCATION_TIME_STRING}_top_paths_snapshot.csv"
            )
        )
    else:
        log.info("do not write df_paths_snapshot_now: empty")

    if args.fork_ts_outpath:
        fetch_and_write_fork_ts(repo, args.fork_ts_outpath)

    if args.stargazer_ts_outpath:
        fetch_and_write_stargazer_ts(repo, args.stargazer_ts_outpath)

    log.info("done!")


def fetch_and_write_stargazer_ts(repo: Repository.Repository, path: str):
    dfstarscsv = get_stars_over_time(repo)
    log.info("stars_cumulative, for CSV file:\n%s", dfstarscsv)
    tpath = path + ".tmp"  # todo: rnd string
    log.info(
        "write stargazer time series to %s, then rename to %s",
        tpath,
        path,
    )
    dfstarscsv.to_csv(tpath, index_label="time_iso8601")
    os.rename(tpath, path)


def fetch_and_write_fork_ts(repo: Repository.Repository, path: str):
    dfforkcsv = get_forks_over_time(repo)
    log.info("forks_cumulative, for CSV file:\n%s", dfforkcsv)
    tpath = path + ".tmp"  # todo: rnd string
    log.info(
        "write fork time series to %s, then rename to %s",
        tpath,
        path,
    )
    dfforkcsv.to_csv(tpath, index_label="time_iso8601")
    os.rename(tpath, path)


def fetch_all_traffic_api_endpoints(
    repo,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:

    log.info("fetch top referrers")
    df_referrers_snapshot_now = referrers_to_df(fetch_top_referrers(repo))

    log.info("fetch top paths")
    df_paths_snapshot_now = paths_to_df(fetch_top_paths(repo))

    log.info("fetch data for clones")
    df_clones = clones_or_views_to_df(fetch_clones(repo), "clones")

    log.info("fetch data for views")
    df_views = clones_or_views_to_df(fetch_views(repo), "views")

    # Note that df_clones and df_views should have the same datetime index, but
    # there is no guarantee for that. Create two separate data frames, then
    # merge / align dynamically.
    if not df_clones.index.equals(df_views.index):
        log.info("special case: df_views and df_clones have different index")
    else:
        log.info("indices of df_views and df_clones are equal")

    log.info("union-merge views and clones")
    # https://pandas.pydata.org/pandas-docs/stable/user_guide/merging.html#set-logic-on-the-other-axes
    # Build union of the two data frames. Zero information loss, in case the
    # two indices aree different.
    df_views_clones = pd.concat([df_clones, df_views], axis=1, join="outer")
    log.info("df_views_clones:\n%s", df_views_clones)

    return df_views_clones, df_referrers_snapshot_now, df_paths_snapshot_now


def parse_args():
    parser = argparse.ArgumentParser(
        description="Fetch traffic data for GitHub repository. Requires the "
        "environment variables GITHUB_USERNAME and GITHUB_APITOKEN to be set."
    )

    parser.add_argument(
        "repo",
        metavar="REPOSITORY",
        help="Owner/organization and repository. Must contain a slash. "
        "Example: coke/truck",
    )

    parser.add_argument(
        "--snapshot-directory",
        type=str,
        default="",
        help="Snapshot/fragment directory. Default: _ghrs_{owner}_{repo}",
    )

    parser.add_argument(
        "--fork-ts-outpath",
        default="",
        metavar="PATH",
        help="Fetch fork time series and write to this CSV file. Overwrite if file exists.",
    )

    parser.add_argument(
        "--stargazer-ts-outpath",
        default="",
        metavar="PATH",
        help="Fetch stargazer time series and write to this CSV file. Overwrite if file exists.",
    )

    args = parser.parse_args()

    if "/" not in args.repo:
        sys.exit("missing slash in REPOSITORY spec")

    ownerid, repoid = args.repo.split("/")
    outdir_path_default = f"_ghrs_{ownerid}_{repoid}"

    if not args.snapshot_directory:
        args.snapshot_directory = outdir_path_default

    log.info("processed args: %s", json.dumps(vars(args), indent=2))

    if os.path.exists(args.snapshot_directory):
        if not os.path.isdir(args.snapshot_directory):
            log.error(
                "the specified output directory path does not point to a directory: %s",
                args.snapshot_directory,
            )
            sys.exit(1)

        log.info("output directory already exists: %s", args.snapshot_directory)

    else:
        log.info("create output directory: %s", args.snapshot_directory)
        log.info("absolute path: %s", os.path.abspath(args.snapshot_directory))
        # If there is a race: do not error out.
        os.makedirs(args.snapshot_directory, exist_ok=True)

    return args


def referrers_to_df(top_referrers) -> pd.DataFrame:
    series_referrers = []
    series_views_unique = []
    series_views_total = []
    for p in top_referrers:
        series_referrers.append(p.referrer)
        series_views_total.append(int(p.count))
        series_views_unique.append(int(p.uniques))

    df = pd.DataFrame(
        data={
            "views_total": series_views_total,
            "views_unique": series_views_unique,
        },
        index=series_referrers,
    )
    df.index.name = "referrer"

    # Attach metadata to dataframe, still experimental -- also see
    # https://stackoverflow.com/q/52122674/145400
    df.attrs["snapshot_time"] = NOW.isoformat()
    return df


def paths_to_df(top_paths) -> pd.DataFrame:

    series_url_paths = []
    series_views_unique = []
    series_views_total = []
    for p in top_paths:
        series_url_paths.append(p.path)
        series_views_total.append(int(p.count))
        series_views_unique.append(int(p.uniques))

    df = pd.DataFrame(
        data={
            "views_total": series_views_total,
            "views_unique": series_views_unique,
        },
        index=series_url_paths,
    )
    df.index.name = "url_path"

    # Attach metadata to dataframe, new as of pandas 1.0 -- also see
    # https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.DataFrame.attrs.html
    # https://github.com/pandas-dev/pandas/issues/28283
    # https://stackoverflow.com/q/52122674/145400
    df.attrs["snapshot_time"] = NOW.isoformat()
    return df


def clones_or_views_to_df(items, metric) -> pd.DataFrame:
    assert metric in ["clones", "views"]

    series_count_total = []
    series_count_unique = []
    series_timestamps = []

    for sample in items:
        # GitHub API docs say "Timestamps are aligned to UTC".
        # `sample.timestamp` is a tz-naive datetime object.
        series_timestamps.append(sample.timestamp)
        series_count_total.append(int(sample.count))
        series_count_unique.append(int(sample.uniques))

    # Attach timezone information to `pd.DatetimeIndex` (make this index
    # tz-aware, leave actual numbers intact).
    df = pd.DataFrame(
        data={
            f"{metric}_total": series_count_total,
            f"{metric}_unique": series_count_unique,
        },
        index=pd.DatetimeIndex(data=series_timestamps, tz="UTC"),
    )
    df.index.name = "time_iso8601"

    log.info("built dataframe for %s:\n%s", metric, df)
    log.info("dataframe datetimeindex detail: %s", df.index)
    return df


def get_forks_over_time(repo: Repository.Repository) -> pd.DataFrame:
    # TODO: for ~10k forks repositories, this operation is too costly for doing
    # it as part of each analyzer invocation. Move this to the fetcher, and
    # persist the data.
    log.info("fetch fork time series for repo %s", repo)

    reqlimit_before = GHUB.get_rate_limit().core.remaining
    log.info("GH request limit before operation: %s", reqlimit_before)

    forks = []
    for count, fork in enumerate(repo.get_forks(), 1):
        # Store `PullRequest` object with integer key in dictionary.
        forks.append(fork)
        if count % 200 == 0:
            log.info("%s forks fetched", count)

    reqlimit_after = GHUB.get_rate_limit().core.remaining
    log.info("GH request limit after operation: %s", reqlimit_after)
    log.info("http requests made (approximately): %s", reqlimit_before - reqlimit_after)
    log.info("current fork count: %s", len(forks))

    # The GitHub API returns ISO 8601 timestamp strings encoding the timezone
    # via the Z suffix, i.e. Zulu time, i.e. UTC. pygithub doesn't parse that
    # timezone. That is, whereas the API returns `starred_at` in UTC, the
    # datetime obj created by pygithub is a naive one. Correct for that.
    forktimes_aware = [pytz.timezone("UTC").localize(f.created_at) for f in forks]

    # Create sorted pandas DatetimeIndex
    dtidx = pd.to_datetime(forktimes_aware)
    dtidx = dtidx.sort_values()

    # Each timestamp corresponds to *1* fork event. Build cumulative sum over
    # time.
    df = pd.DataFrame(
        data={"fork_events": [1] * len(forks)},
        index=dtidx,
    )
    df.index.name = "time"
    df["forks_cumulative"] = df["fork_events"].cumsum()
    df = df.drop(columns=["fork_events"]).astype(int)
    log.info("forks df: \n%s", df)
    return df


def get_stars_over_time(repo: Repository.Repository) -> pd.DataFrame:
    # TODO: for ~10k stars repositories, this operation is too costly for doing
    # it as part of each analyzer invocation. Move this to the fetcher, and
    # persist the data.
    log.info("fetch stargazer time series for repo %s", repo)

    reqlimit_before = GHUB.get_rate_limit().core.remaining

    log.info("GH request limit before fetch operation: %s", reqlimit_before)

    gazers = []

    # TODO for addressing the 10ks challenge: save state to disk, and refresh
    # using reverse order iteration. See for repo in user.get_repos().reversed
    for count, gazer in enumerate(repo.get_stargazers_with_dates(), 1):
        # Store `PullRequest` object with integer key in dictionary.
        gazers.append(gazer)
        if count % 200 == 0:
            log.info("%s gazers fetched", count)

    reqlimit_after = GHUB.get_rate_limit().core.remaining
    log.info("GH request limit after fetch operation: %s", reqlimit_after)
    log.info("http requests made (approximately): %s", reqlimit_before - reqlimit_after)
    log.info("stargazer count: %s", len(gazers))

    # The GitHub API returns ISO 8601 timestamp strings encoding the timezone
    # via the Z suffix, i.e. Zulu time, i.e. UTC. pygithub doesn't parze that
    # timezone. That is, whereas the API returns `starred_at` in UTC, the
    # datetime obj created by pygithub is a naive one. Correct for that.
    startimes_aware = [pytz.timezone("UTC").localize(g.starred_at) for g in gazers]

    # Work towards a dataframe of the following shape:
    #                            star_events  stars_cumulative
    # time
    # 2020-11-26 16:25:37+00:00            1                 1
    # 2020-11-26 16:27:23+00:00            1                 2
    # 2020-11-26 16:30:05+00:00            1                 3
    # 2020-11-26 17:31:57+00:00            1                 4
    # 2020-11-26 17:48:48+00:00            1                 5
    # ...                                ...               ...
    # 2020-12-19 19:48:58+00:00            1               327
    # 2020-12-22 04:44:35+00:00            1               328
    # 2020-12-22 19:00:42+00:00            1               329
    # 2020-12-25 05:01:42+00:00            1               330
    # 2020-12-28 01:07:55+00:00            1               331

    # Create sorted pandas DatetimeIndex
    dtidx = pd.to_datetime(startimes_aware)
    dtidx = dtidx.sort_values()

    # Each timestamp corresponds to *1* star event. Build cumulative sum over
    # time.
    df = pd.DataFrame(
        data={"star_events": [1] * len(gazers)},
        index=dtidx,
    )
    df.index.name = "time"

    df["stars_cumulative"] = df["star_events"].cumsum()
    df = df.drop(columns=["star_events"]).astype(int)
    log.info("stargazer df\n %s", df)
    return df


def handle_rate_limit_error(exc):

    if "wait a few minutes before you try again" in str(exc):
        log.warning("GitHub abuse mechanism triggered, wait 60 s, retry")
        return True

    if "403" in str(exc):
        if "Resource not accessible by integration" in str(exc):
            log.error(
                'this appears to be a permanent error, as in "access denied -- do not retry": %s',
                str(exc),
            )
            sys.exit(1)

        log.warning("Exception contains 403, wait 60 s, retry: %s", str(exc))
        # The request count quota is not necessarily responsible for this
        # exception, but it usually is. Log the expected local time when the
        # new quota arrives.
        unix_timestamp_quota_reset = GHUB.rate_limiting_resettime
        local_time = datetime.fromtimestamp(unix_timestamp_quota_reset)
        log.info("New req count quota at: %s", local_time.strftime("%Y-%m-%d %H:%M:%S"))
        return True

    # For example, `RemoteDisconnected` is a case I have seen in production.
    if isinstance(exc, requests.exceptions.RequestException):
        log.warning("RequestException, wait 60 s, retry: %s", str(exc))
        return True

    return False


@retrying.retry(wait_fixed=60000, retry_on_exception=handle_rate_limit_error)
def fetch_clones(repo):
    clones = repo.get_clones_traffic()
    return clones["clones"]


@retrying.retry(wait_fixed=60000, retry_on_exception=handle_rate_limit_error)
def fetch_views(repo):
    views = repo.get_views_traffic()
    return views["views"]


@retrying.retry(wait_fixed=60000, retry_on_exception=handle_rate_limit_error)
def fetch_top_referrers(repo):
    return repo.get_top_referrers()


@retrying.retry(wait_fixed=60000, retry_on_exception=handle_rate_limit_error)
def fetch_top_paths(repo):
    return repo.get_top_paths()


if __name__ == "__main__":
    main()
