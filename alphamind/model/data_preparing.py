# -*- coding: utf-8 -*-
"""
Created on 2017-8-24

@author: cheng.li
"""

import numpy as np
import pandas as pd
from typing import Iterable
from typing import Union
from PyFin.api import makeSchedule
from PyFin.api import BizDayConventions
from PyFin.api import advanceDateByCalendar
from PyFin.DateUtilities import Period
from PyFin.Enums import TimeUnits
from alphamind.data.transformer import Transformer
from alphamind.data.engines.sqlengine import SqlEngine
from alphamind.data.engines.universe import Universe
from alphamind.data.processing import factor_processing
from alphamind.utilities import alpha_logger


def _map_horizon(frequency: str) -> int:
    parsed_period = Period(frequency)
    unit = parsed_period.units()
    length = parsed_period.length()
    if unit == TimeUnits.BDays or unit == TimeUnits.Days:
        return length - 1
    elif unit == TimeUnits.Weeks:
        return 5 * length - 1
    elif unit == TimeUnits.Months:
        return 22 * length - 1
    else:
        raise ValueError('{0} is an unrecognized frequency rule'.format(frequency))


def prepare_data(engine: SqlEngine,
                 factors: Union[Transformer, Iterable[object]],
                 start_date: str,
                 end_date: str,
                 frequency: str,
                 universe: Universe,
                 benchmark: int,
                 warm_start: int = 0):
    if warm_start > 0:
        start_date = advanceDateByCalendar('china.sse', start_date, str(-warm_start) + 'b').strftime('%Y-%m-%d')

    dates = makeSchedule(start_date, end_date, frequency, calendar='china.sse', dateRule=BizDayConventions.Following)

    horizon = _map_horizon(frequency)

    if isinstance(factors, Transformer):
        transformer = factors
    else:
        transformer = Transformer(factors)

    factor_df = engine.fetch_factor_range(universe,
                                          factors=transformer,
                                          dates=dates).sort_values(['trade_date', 'code'])
    return_df = engine.fetch_dx_return_range(universe, dates=dates, horizon=horizon)
    industry_df = engine.fetch_industry_range(universe, dates=dates)
    benchmark_df = engine.fetch_benchmark_range(benchmark, dates=dates)

    df = pd.merge(factor_df, return_df, on=['trade_date', 'code']).dropna()
    df = pd.merge(df, benchmark_df, on=['trade_date', 'code'], how='left')
    df = pd.merge(df, industry_df, on=['trade_date', 'code'])
    df['weight'] = df['weight'].fillna(0.)

    return dates, df[['trade_date', 'code', 'dx']], df[
        ['trade_date', 'code', 'weight', 'isOpen', 'industry_code', 'industry'] + transformer.names]


def batch_processing(x_values,
                     y_values,
                     groups,
                     group_label,
                     batch,
                     risk_exp,
                     pre_process,
                     post_process):
    train_x_buckets = {}
    train_y_buckets = {}
    predict_x_buckets = {}
    predict_y_buckets = {}

    for i, start in enumerate(groups[:-batch]):
        end = groups[i + batch]
        index = (group_label >= start) & (group_label < end)
        this_raw_x = x_values[index]
        this_raw_y = y_values[index]
        if risk_exp is not None:
            this_risk_exp = risk_exp[index]
        else:
            this_risk_exp = None

        train_x_buckets[end] = factor_processing(this_raw_x,
                                                 pre_process=pre_process,
                                                 risk_factors=this_risk_exp,
                                                 post_process=post_process)

        train_y_buckets[end] = factor_processing(this_raw_y,
                                                 pre_process=pre_process,
                                                 risk_factors=this_risk_exp,
                                                 post_process=post_process)

        index = (group_label > start) & (group_label <= end)
        sub_dates = group_label[index]
        this_raw_x = x_values[index]

        if risk_exp is not None:
            this_risk_exp = risk_exp[index]
        else:
            this_risk_exp = None

        ne_x = factor_processing(this_raw_x,
                                 pre_process=pre_process,
                                 risk_factors=this_risk_exp,
                                 post_process=post_process)
        predict_x_buckets[end] = ne_x[sub_dates == end]

        this_raw_y = y_values[index]
        if len(this_raw_y) > 0:
            ne_y = factor_processing(this_raw_y,
                                     pre_process=pre_process,
                                     risk_factors=this_risk_exp,
                                     post_process=post_process)
            predict_y_buckets[end] = ne_y[sub_dates == end]

    return train_x_buckets, train_y_buckets, predict_x_buckets, predict_y_buckets


def fetch_data_package(engine: SqlEngine,
                       alpha_factors: Iterable[object],
                       start_date: str,
                       end_date: str,
                       frequency: str,
                       universe: Universe,
                       benchmark: int,
                       warm_start: int = 0,
                       batch: int = 1,
                       neutralized_risk: Iterable[str] = None,
                       risk_model: str = 'short',
                       pre_process: Iterable[object] = None,
                       post_process: Iterable[object] = None):
    alpha_logger.info("Starting data package fetching ...")

    transformer = Transformer(alpha_factors)
    dates, return_df, factor_df = prepare_data(engine,
                                               transformer,
                                               start_date,
                                               end_date,
                                               frequency,
                                               universe,
                                               benchmark,
                                               warm_start)

    if neutralized_risk:
        risk_df = engine.fetch_risk_model_range(universe, dates=dates, risk_model=risk_model)[1]
        used_neutralized_risk = list(set(neutralized_risk).difference(transformer.names))
        risk_df = risk_df[['trade_date', 'code'] + used_neutralized_risk].dropna()

        train_x = pd.merge(factor_df, risk_df, on=['trade_date', 'code'])
        return_df = pd.merge(return_df, risk_df, on=['trade_date', 'code'])[['trade_date', 'code', 'dx']]
        train_y = return_df.copy()

        risk_exp = train_x[neutralized_risk].values.astype(float)
        x_values = train_x[transformer.names].values.astype(float)
        y_values = train_y[['dx']].values
    else:
        risk_exp = None
        train_x = factor_df.copy()
        train_y = return_df.copy()
        x_values = train_x[transformer.names].values.astype(float)
        y_values = train_y[['dx']].values

    date_label = pd.DatetimeIndex(factor_df.trade_date).to_pydatetime()
    dates = np.unique(date_label)

    return_df['weight'] = train_x['weight']
    return_df['industry'] = train_x['industry']
    return_df['industry_code'] = train_x['industry_code']
    return_df['isOpen'] = train_x['isOpen']
    for i, name in enumerate(neutralized_risk):
        return_df.loc[:, name] = risk_exp[:, i]

    alpha_logger.info("Loading data is finished")

    train_x_buckets, train_y_buckets, predict_x_buckets, predict_y_buckets = batch_processing(x_values,
                                                                                              y_values,
                                                                                              dates,
                                                                                              date_label,
                                                                                              batch,
                                                                                              risk_exp,
                                                                                              pre_process,
                                                                                              post_process)

    alpha_logger.info("Data processing is finished")

    ret = dict()
    ret['x_names'] = transformer.names
    ret['settlement'] = return_df
    ret['train'] = {'x': train_x_buckets, 'y': train_y_buckets}
    ret['predict'] = {'x': predict_x_buckets, 'y': predict_y_buckets}
    return ret


if __name__ == '__main__':
    from PyFin.api import MA

    engine = SqlEngine('postgresql+psycopg2://postgres:A12345678!@10.63.6.220/alpha')
    universe = Universe('zz500', ['zz500'])
    res = fetch_data_package(engine,
                             MA(10, 'EPS'),
                             '2012-01-01',
                             '2012-04-01',
                             '1m',
                             universe,
                             905,
                             0)

    print(res)