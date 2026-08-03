import test from 'node:test';
import assert from 'node:assert/strict';
import { createDatabase } from './db.mjs';
import { login } from './domain.mjs';
import { forecastDemand, latestForecast } from './forecast.mjs';

test('统计预测使用数据库历史记录并持久化版本和置信区间',()=>{
  const db=createDatabase(':memory:');
  const user=login(db,'DEMO','manager','Demo@2026').user;
  const first=forecastDemand(db,user);
  const second=forecastDemand(db,user);
  assert.equal(first.algorithm,'weighted_moving_average_with_trend');
  assert.equal(first.features.sampleSize,4);
  assert.ok(first.lowerBound<first.predictedTraffic);
  assert.ok(first.upperBound>first.predictedTraffic);
  assert.equal(second.version,2);
  assert.equal(latestForecast(db,user).id,second.id);
});
