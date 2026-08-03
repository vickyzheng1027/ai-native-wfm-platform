import { randomUUID } from 'node:crypto';

export function forecastDemand(db, user, eventId = 'event-member-day') {
  const event = db.prepare('SELECT * FROM events WHERE id=? AND tenant_id=?').get(eventId,user.tenantId);
  if (!event) throw Object.assign(new Error('活动不存在'),{ status:404,code:'NOT_FOUND' });
  const history = db.prepare(`SELECT * FROM demand_history WHERE tenant_id=? AND store_id=?
    ORDER BY business_date DESC LIMIT 12`).all(user.tenantId,event.store_id);
  if (history.length < 3) throw Object.assign(new Error('历史数据不足，至少需要 3 条记录'),{ status:409,code:'INSUFFICIENT_HISTORY' });
  const comparable = history.filter(row => row.event_type === 'member_day');
  const sample = comparable.length >= 3 ? comparable : history;
  const chronological = [...sample].sort((a,b) => a.business_date.localeCompare(b.business_date));
  const weights = chronological.map((_,index) => index + 1);
  const weightTotal = weights.reduce((sum,value) => sum + value,0);
  const baseline = chronological.reduce((sum,row,index) => sum + row.traffic * weights[index],0) / weightTotal;
  const first = chronological[0].traffic;
  const last = chronological.at(-1).traffic;
  const trend = chronological.length > 1 ? (last - first) / (chronological.length - 1) : 0;
  const predictedTraffic = Math.round(baseline + trend);
  const deviations = chronological.map(row => Math.abs(row.traffic - baseline));
  const meanDeviation = deviations.reduce((sum,value) => sum + value,0) / deviations.length;
  const margin = Math.max(50,Math.round(meanDeviation * 1.64));
  const trafficPerEmployee = chronological.reduce((sum,row) => sum + row.traffic / row.required_headcount,0) / chronological.length;
  const requiredHeadcount = Math.ceil(predictedTraffic / trafficPerEmployee);
  const confidence = Math.min(0.95,Math.round((0.6 + chronological.length * 0.04) * 100) / 100);
  const previousVersion = db.prepare('SELECT COALESCE(MAX(version),0) AS version FROM demand_forecasts WHERE tenant_id=? AND store_id=? AND forecast_date=?').get(user.tenantId,event.store_id,event.event_date).version;
  const result = {
    id:randomUUID(), eventId, storeId:event.store_id, forecastDate:event.event_date,
    predictedTraffic, lowerBound:Math.max(0,predictedTraffic-margin), upperBound:predictedTraffic+margin,
    requiredHeadcount, algorithm:'weighted_moving_average_with_trend', confidence,
    version:previousVersion+1,
    features:{ sampleSize:chronological.length, eventType:'member_day', weightedBaseline:Number(baseline.toFixed(2)), trendPerPeriod:Number(trend.toFixed(2)), trafficPerEmployee:Number(trafficPerEmployee.toFixed(2)), sourceRows:chronological.map(row => row.id) }
  };
  db.prepare(`INSERT INTO demand_forecasts(id,tenant_id,store_id,event_id,forecast_date,predicted_traffic,lower_bound,upper_bound,required_headcount,algorithm,features_json,confidence,version,created_at)
    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)`).run(result.id,user.tenantId,result.storeId,eventId,result.forecastDate,result.predictedTraffic,result.lowerBound,result.upperBound,result.requiredHeadcount,result.algorithm,JSON.stringify(result.features),result.confidence,result.version,new Date().toISOString());
  return result;
}

export function latestForecast(db,user,eventId='event-member-day') {
  const row = db.prepare(`SELECT * FROM demand_forecasts WHERE tenant_id=? AND event_id=? ORDER BY version DESC LIMIT 1`).get(user.tenantId,eventId);
  return row ? { id:row.id,eventId:row.event_id,forecastDate:row.forecast_date,predictedTraffic:row.predicted_traffic,lowerBound:row.lower_bound,upperBound:row.upper_bound,requiredHeadcount:row.required_headcount,algorithm:row.algorithm,features:JSON.parse(row.features_json),confidence:row.confidence,version:row.version } : null;
}
