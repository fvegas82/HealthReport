package es.fvegas.healthhubbridge

import android.content.Context
import androidx.health.connect.client.HealthConnectClient
import androidx.health.connect.client.records.Record
import androidx.health.connect.client.request.ReadRecordsRequest
import androidx.health.connect.client.time.TimeRangeFilter
import androidx.work.CoroutineWorker
import androidx.work.ExistingWorkPolicy
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import java.time.Instant
import java.time.temporal.ChronoUnit
import java.util.concurrent.TimeUnit

class HealthSyncWorker(appContext: Context, params: WorkerParameters) : CoroutineWorker(appContext, params) {
    override suspend fun doWork(): Result {
        val prefs = applicationContext.getSharedPreferences("bridge", Context.MODE_PRIVATE)
        val endpoint = prefs.getString("endpoint", "") ?: ""
        val key = prefs.getString("key", "") ?: ""
        val accessClientId = prefs.getString("accessClientId", "") ?: ""
        val accessClientSecret = prefs.getString("accessClientSecret", "") ?: ""
        if (endpoint.isBlank() || key.isBlank() || accessClientId.isBlank() || accessClientSecret.isBlank()) {
            prefs.edit().putString(
                "lastSyncStatus",
                "Configuración incompleta: revisa dirección, clave y credenciales de Cloudflare Access."
            ).apply()
            return Result.failure()
        }
        return try {
            val client = HealthConnectClient.getOrCreate(applicationContext)
            val requestedBackfill = inputData.getBoolean("backfill30Days", false)
            // A first automatic run used to attempt 30 days of granular records.
            // On a phone with minute-by-minute steps this can exceed Android's
            // background-work limit before it ever reaches today's records, then
            // repeat forever because the completion flag was never written.
            // Keep the normal job intentionally small and leave the explicit
            // "Reimportar" action as the only full-history operation.
            val start = Instant.now().minus(
                if (requestedBackfill) 30 else 3,
                if (requestedBackfill) ChronoUnit.DAYS else ChronoUnit.HOURS
            )
            HealthRepository(client, endpoint, key, accessClientId, accessClientSecret).syncRecent(start)
            prefs.edit()
                .putBoolean("initialBackfillCompleted", true)
                .putString("lastSyncStatus", "Última sincronización correcta: ${java.time.Instant.now()}")
                .apply()
            Result.success()
        } catch (e: Exception) {
            prefs.edit().putString(
                "lastSyncStatus",
                "Error de sincronización: ${e.javaClass.simpleName}${e.message?.let { ": $it" } ?: ""}"
            ).apply()
            Result.retry()
        }
    }

    companion object {
        fun schedule(context: Context) {
            val immediate = OneTimeWorkRequestBuilder<HealthSyncWorker>().build()
            WorkManager.getInstance(context).enqueueUniqueWork(
                "healthhub-sync-now", ExistingWorkPolicy.REPLACE, immediate
            )
            val request = PeriodicWorkRequestBuilder<HealthSyncWorker>(1, TimeUnit.HOURS).build()
            WorkManager.getInstance(context).enqueueUniquePeriodicWork(
                "healthhub-sync", ExistingPeriodicWorkPolicy.UPDATE, request
            )
        }

        fun backfill30Days(context: Context) {
            val request = OneTimeWorkRequestBuilder<HealthSyncWorker>()
                .setInputData(androidx.work.workDataOf("backfill30Days" to true))
                .build()
            WorkManager.getInstance(context).enqueueUniqueWork(
                "healthhub-sync-backfill", ExistingWorkPolicy.REPLACE, request
            )
        }
    }
}
