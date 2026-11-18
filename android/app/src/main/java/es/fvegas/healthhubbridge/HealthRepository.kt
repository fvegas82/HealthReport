package es.fvegas.healthhubbridge

import androidx.health.connect.client.HealthConnectClient
import androidx.health.connect.client.records.*
import androidx.health.connect.client.request.AggregateRequest
import androidx.health.connect.client.request.ReadRecordsRequest
import androidx.health.connect.client.time.TimeRangeFilter
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.time.Instant
import java.time.Duration
import java.time.ZoneId

class HealthRepository(
    private val client: HealthConnectClient,
    private val endpoint: String,
    private val key: String,
    private val accessClientId: String,
    private val accessClientSecret: String,
) {
    private val http = OkHttpClient()
    private val json = "application/json".toMediaType()

    suspend fun syncRecent(start: Instant) {
        sync(WeightRecord::class, start, { it.time }) { r -> JSONObject().put("kg", r.weight.inKilograms) }
        sync(BodyFatRecord::class, start, { it.time }) { r -> JSONObject().put("percent", r.percentage.value * 100) }
        sync(LeanBodyMassRecord::class, start, { it.time }) { r -> JSONObject().put("kg", r.mass.inKilograms) }
        sync(BoneMassRecord::class, start, { it.time }) { r -> JSONObject().put("kg", r.mass.inKilograms) }
        sync(BodyWaterMassRecord::class, start, { it.time }) { r -> JSONObject().put("kg", r.mass.inKilograms) }
        // Steps from different apps can overlap. Reading every StepsRecord and
        // adding it in the report counts those overlaps twice. Health Connect's
        // aggregate API applies the user's activity-source priority and returns
        // the same deduplicated total that Health Connect presents to the user.
        syncDailySteps(start)
        sync(DistanceRecord::class, start, { it.startTime }) { r -> JSONObject().put("meters", r.distance.inMeters) }
        sync(ActiveCaloriesBurnedRecord::class, start, { it.startTime }) { r -> JSONObject().put("kcal", r.energy.inKilocalories) }
        sync(TotalCaloriesBurnedRecord::class, start, { it.startTime }) { r -> JSONObject().put("kcal", r.energy.inKilocalories) }
        sync(RestingHeartRateRecord::class, start, { it.time }) { r -> JSONObject().put("bpm", r.beatsPerMinute) }
        sync(HeartRateVariabilityRmssdRecord::class, start, { it.time }) { r -> JSONObject().put("ms", r.heartRateVariabilityMillis) }
        sync(OxygenSaturationRecord::class, start, { it.time }) { r -> JSONObject().put("percent", r.percentage.value * 100) }
        sync(RespiratoryRateRecord::class, start, { it.time }) { r -> JSONObject().put("rpm", r.rate) }
        sync(HeartRateRecord::class, start, { it.startTime }) { r ->
            val values = r.samples.map { it.beatsPerMinute }
            JSONObject().put("end", r.endTime.toString())
                .put("avg_bpm", values.average())
                .put("min_bpm", values.minOrNull())
                .put("max_bpm", values.maxOrNull())
                .put("samples", values.size)
        }
        sync(SleepSessionRecord::class, start, { it.startTime }) { r ->
            JSONObject().put("end", r.endTime.toString())
                .put("duration_minutes", Duration.between(r.startTime, r.endTime).toMinutes())
                .put("stages", r.stages.size)
        }
        sync(ExerciseSessionRecord::class, start, { it.startTime }) { r ->
            JSONObject().put("end", r.endTime.toString())
                .put("type", r.exerciseType)
                .put("duration_minutes", Duration.between(r.startTime, r.endTime).toMinutes())
        }
    }

    private suspend fun syncDailySteps(start: Instant) {
        val zone = ZoneId.systemDefault()
        var day = start.atZone(zone).toLocalDate()
        val today = Instant.now().atZone(zone).toLocalDate()
        while (!day.isAfter(today)) {
            val dayStart = day.atStartOfDay(zone).toInstant()
            val dayEnd = day.plusDays(1).atStartOfDay(zone).toInstant()
            val result = client.aggregate(
                AggregateRequest(
                    metrics = setOf(StepsRecord.COUNT_TOTAL),
                    timeRangeFilter = TimeRangeFilter.between(dayStart, dayEnd),
                )
            )
            val count = result[StepsRecord.COUNT_TOTAL] ?: 0L
            val body = JSONObject()
                .put("source", "health_connect")
                .put("event_type", "DailyStepsTotal")
                .put("occurred_at", dayStart.toString())
                .put("count", count)
                .put("day", day.toString())
                .put("aggregation", "health_connect_priority")
            post(body)
            day = day.plusDays(1)
        }
    }

    private suspend inline fun <reified T : Record> sync(
        type: kotlin.reflect.KClass<T>,
        start: Instant,
        crossinline occurredAt: (T) -> Instant,
        crossinline payload: (T) -> JSONObject,
    ) {
        var token: String? = null
        do {
            val response = client.readRecords(
                ReadRecordsRequest(recordType = type, timeRangeFilter = TimeRangeFilter.after(start), pageToken = token)
            )
            response.records.forEach { record ->
                val body = payload(record).put("source", "health_connect")
                    .put("event_type", type.simpleName).put("occurred_at", occurredAt(record).toString())
                post(body)
            }
            token = response.pageToken
        } while (!token.isNullOrBlank())
    }

    private fun post(body: JSONObject) {
        val request = Request.Builder().url("$endpoint/v1/ingest")
            .header("x-api-key", key)
            .header("CF-Access-Client-Id", accessClientId)
            .header("CF-Access-Client-Secret", accessClientSecret)
            .post(body.toString().toRequestBody(json)).build()
        http.newCall(request).execute().use { response -> check(response.isSuccessful) }
    }
}
