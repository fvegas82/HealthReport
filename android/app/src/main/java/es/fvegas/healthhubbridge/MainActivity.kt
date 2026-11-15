package es.fvegas.healthhubbridge

import android.os.Bundle
import android.text.InputType
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import androidx.activity.ComponentActivity
import androidx.activity.result.contract.ActivityResultContracts
import androidx.health.connect.client.HealthConnectClient
import androidx.health.connect.client.PermissionController
import androidx.health.connect.client.permission.HealthPermission
import androidx.health.connect.client.records.DistanceRecord
import androidx.health.connect.client.records.BodyFatRecord
import androidx.health.connect.client.records.LeanBodyMassRecord
import androidx.health.connect.client.records.BoneMassRecord
import androidx.health.connect.client.records.BodyWaterMassRecord
import androidx.health.connect.client.records.ActiveCaloriesBurnedRecord
import androidx.health.connect.client.records.ExerciseSessionRecord
import androidx.health.connect.client.records.HeartRateRecord
import androidx.health.connect.client.records.HeartRateVariabilityRmssdRecord
import androidx.health.connect.client.records.OxygenSaturationRecord
import androidx.health.connect.client.records.RespiratoryRateRecord
import androidx.health.connect.client.records.RestingHeartRateRecord
import androidx.health.connect.client.records.SleepSessionRecord
import androidx.health.connect.client.records.StepsRecord
import androidx.health.connect.client.records.TotalCaloriesBurnedRecord
import androidx.health.connect.client.records.WeightRecord
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity() {
    private lateinit var status: TextView
    private val permissions = setOf(
        HealthPermission.getReadPermission(WeightRecord::class),
        HealthPermission.getReadPermission(BodyFatRecord::class),
        HealthPermission.getReadPermission(LeanBodyMassRecord::class),
        HealthPermission.getReadPermission(BoneMassRecord::class),
        HealthPermission.getReadPermission(BodyWaterMassRecord::class),
        HealthPermission.getReadPermission(StepsRecord::class),
        HealthPermission.getReadPermission(DistanceRecord::class),
        HealthPermission.getReadPermission(ActiveCaloriesBurnedRecord::class),
        HealthPermission.getReadPermission(TotalCaloriesBurnedRecord::class),
        HealthPermission.getReadPermission(ExerciseSessionRecord::class),
        HealthPermission.getReadPermission(HeartRateRecord::class),
        HealthPermission.getReadPermission(RestingHeartRateRecord::class),
        HealthPermission.getReadPermission(HeartRateVariabilityRmssdRecord::class),
        HealthPermission.getReadPermission(OxygenSaturationRecord::class),
        HealthPermission.getReadPermission(RespiratoryRateRecord::class),
        HealthPermission.getReadPermission(SleepSessionRecord::class),
        HealthPermission.PERMISSION_READ_HEALTH_DATA_IN_BACKGROUND,
        HealthPermission.PERMISSION_READ_HEALTH_DATA_HISTORY
    )

    private val permissionsLauncher = registerForActivityResult(
        PermissionController.createRequestPermissionResultContract()
    ) { granted ->
        status.text = if (granted.containsAll(permissions))
            "Permisos concedidos. Puedes sincronizar."
        else "Faltan permisos. Health Connect permite conceder solo los que prefieras."
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val prefs = getSharedPreferences("bridge", MODE_PRIVATE)
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 48, 48, 48)
        }
        val title = TextView(this).apply { text = "Health Hub Bridge"; textSize = 24f }
        val endpoint = EditText(this).apply {
            hint = "Dirección privada del Health Hub"
            setText(prefs.getString("endpoint", ""))
        }
        val key = EditText(this).apply {
            hint = "Clave de sincronización"
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
            setText(prefs.getString("key", ""))
        }
        val accessClientId = EditText(this).apply {
            hint = "Cloudflare Access Client ID"
            setText(prefs.getString("accessClientId", ""))
        }
        val accessClientSecret = EditText(this).apply {
            hint = "Cloudflare Access Client Secret"
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
            setText(prefs.getString("accessClientSecret", ""))
        }
        status = TextView(this).apply { text = "Aún no configurado." }
        val grant = Button(this).apply { text = "Conceder permisos de Health Connect" }
        val save = Button(this).apply { text = "Guardar y activar sincronización" }
        val backfill = Button(this).apply { text = "Reimportar últimos 30 días" }
        root.addView(title); root.addView(endpoint); root.addView(key); root.addView(accessClientId); root.addView(accessClientSecret)
        root.addView(grant); root.addView(save); root.addView(backfill); root.addView(status)
        setContentView(root)

        grant.setOnClickListener { permissionsLauncher.launch(permissions) }
        save.setOnClickListener {
            prefs.edit().putString("endpoint", endpoint.text.toString().trim().removeSuffix("/"))
                .putString("key", key.text.toString().trim())
                .putString("accessClientId", accessClientId.text.toString().trim())
                .putString("accessClientSecret", accessClientSecret.text.toString().trim()).apply()
            lifecycleScope.launch {
                val client = HealthConnectClient.getOrCreate(this@MainActivity)
                val granted = client.permissionController.getGrantedPermissions()
                if (!granted.containsAll(permissions - HealthPermission.PERMISSION_READ_HEALTH_DATA_IN_BACKGROUND)) {
                    status.text = "Primero concede los permisos de Health Connect."
                } else {
                    HealthSyncWorker.schedule(this@MainActivity)
                    status.text = "Sincronización activada: se ejecutará periódicamente en segundo plano."
                }
            }
        }
        backfill.setOnClickListener {
            lifecycleScope.launch {
                val client = HealthConnectClient.getOrCreate(this@MainActivity)
                val granted = client.permissionController.getGrantedPermissions()
                if (!granted.containsAll(permissions - HealthPermission.PERMISSION_READ_HEALTH_DATA_IN_BACKGROUND)) {
                    status.text = "Primero concede los permisos de Health Connect."
                } else {
                    HealthSyncWorker.backfill30Days(this@MainActivity)
                    status.text = "Reimportación de 30 días iniciada. Puede tardar unos minutos."
                }
            }
        }
    }
}
