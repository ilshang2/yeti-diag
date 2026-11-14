package io.github.ilshang2.yetidiag

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.app.AlertDialog
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothManager
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Typeface
import android.os.Build
import android.os.Bundle
import android.view.Gravity
import android.view.View
import android.view.ViewGroup.LayoutParams.MATCH_PARENT
import android.view.ViewGroup.LayoutParams.WRAP_CONTENT
import android.view.WindowInsets
import android.view.WindowManager
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.GridLayout
import android.text.InputType
import android.widget.LinearLayout
import android.widget.RadioButton
import android.widget.RadioGroup
import android.widget.ScrollView
import android.widget.Spinner
import android.widget.TextView
import android.widget.Toast
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Phone front end of yeti_diag. Pages: fault codes, live engine values, battery, hidden
 * functions, command line. The Python core (yeti_app.py) does all the car talk; a write is
 * always plan -> dialog with old / new -> apply, sent once.
 */
class MainActivity : Activity() {
    private val worker = Executors.newSingleThreadExecutor()
    private var link: BtLink? = null
    private var devices: List<BluetoothDevice> = emptyList()
    private var busy = false
    private var liveRunning = false
    private val liveStop = AtomicBoolean(false)
    private var connectedText = "未連線"
    private var progress = ""
    private var scanReport = ""

    private lateinit var deviceSpinner: Spinner
    private lateinit var connectButton: Button
    private lateinit var stopButton: Button
    private lateinit var status: TextView
    private lateinit var log: TextView
    private lateinit var logScroll: ScrollView
    private lateinit var argsField: EditText
    private lateinit var scanSummary: TextView
    private lateinit var scanList: LinearLayout
    private lateinit var liveGrid: GridLayout
    private lateinit var liveNote: TextView
    private lateinit var batteryText: TextView
    private lateinit var featureNote: TextView
    private lateinit var featureList: LinearLayout
    private lateinit var serviceText: TextView
    private var serviceState = JSONObject()
    private val pages = mutableListOf<View>()
    private val tabs = mutableListOf<Button>()
    private val actionButtons = mutableListOf<Button>()   // fixed buttons that start a command
    private val listButtons = mutableListOf<Button>()     // buttons inside the scan / feature lists
    private val liveTiles = HashMap<String, TextView>()

    private val dp by lazy { resources.displayMetrics.density }
    private fun px(v: Int) = (v * dp).toInt()
    private fun py(): PyObject = Python.getInstance().getModule("yeti_app")

    /** Python prints through this (worker thread). `mirror` also shows the text on a page. */
    inner class Sink(private val mirror: TextView? = null) {
        fun append(text: String) = runOnUiThread {
            appendLog(text)
            mirror?.append(text)
            text.trim().lines().lastOrNull { it.isNotBlank() }?.let { progress = it.trim() }
        }

        fun update(json: String) = runOnUiThread { showLive(JSONArray(json)) }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)  // screen-off drops Bluetooth
        if (!Python.isStarted()) Python.start(AndroidPlatform(this))
        setContentView(buildUi())
        showPage(0)
        setBusy(false)
        if (Build.VERSION.SDK_INT >= 31 &&
            checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) != PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.BLUETOOTH_CONNECT), 1)
        }
    }

    /** Reload the paired list every time the app comes back (Bluetooth turned on, adapter just paired). */
    override fun onResume() {
        super.onResume()
        val allowed = Build.VERSION.SDK_INT < 31 ||
            checkSelfPermission(Manifest.permission.BLUETOOTH_CONNECT) == PackageManager.PERMISSION_GRANTED
        if (allowed && link?.closed != false) loadDevices()
    }

    override fun onRequestPermissionsResult(code: Int, perms: Array<out String>, results: IntArray) {
        if (results.firstOrNull() != PackageManager.PERMISSION_GRANTED) appendLog("沒有藍牙權限，無法連接接頭。\n")
    }

    override fun onDestroy() {
        liveStop.set(true)
        link?.close()
        worker.shutdownNow()
        super.onDestroy()
    }

    // ---------------------------------------------------------------- layout

    private fun buildUi(): View {
        val pad = px(10)
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad, pad, pad)
            // Android 15 (targetSdk 35) draws the app under the status and navigation bars;
            // without this the device row is hidden behind them.
            setOnApplyWindowInsetsListener { v, insets ->
                if (Build.VERSION.SDK_INT >= 30) {
                    val b = insets.getInsets(
                        WindowInsets.Type.systemBars() or WindowInsets.Type.displayCutout() or WindowInsets.Type.ime()
                    )
                    v.setPadding(pad + b.left, pad + b.top, pad + b.right, pad + b.bottom)
                } else {
                    @Suppress("DEPRECATION")
                    v.setPadding(
                        pad + insets.systemWindowInsetLeft, pad + insets.systemWindowInsetTop,
                        pad + insets.systemWindowInsetRight, pad + insets.systemWindowInsetBottom
                    )
                }
                insets
            }
        }

        deviceSpinner = Spinner(this)
        connectButton = plainButton("連線") { connect() }
        root.addView(row(deviceSpinner, connectButton))

        status = TextView(this).apply { text = connectedText; textSize = 13f }
        stopButton = plainButton("停止") { stop() }
        root.addView(row(status, stopButton).apply { gravity = Gravity.CENTER_VERTICAL })

        val tabRow = LinearLayout(this)
        val names = listOf("故障碼", "即時", "電瓶", "功能", "保養", "進階")
        names.forEachIndexed { i, name ->
            val b = plainButton(name) { showPage(i) }.apply { setPadding(0, 0, 0, 0) }
            tabs += b
            tabRow.addView(b, LinearLayout.LayoutParams(0, WRAP_CONTENT, 1f))
        }
        root.addView(tabRow)

        val frame = FrameLayout(this)
        pages += listOf(scanPage(), livePage(), batteryPage(), featurePage(), servicePage(), advancedPage())
        pages.forEach { frame.addView(it, FrameLayout.LayoutParams(MATCH_PARENT, MATCH_PARENT)) }
        root.addView(frame, LinearLayout.LayoutParams(MATCH_PARENT, 0, 1f))
        return root
    }

    private fun scanPage(): View {
        val page = column()
        page.addView(buttonRow(
            actionButton("全車掃描") { scan() },
            plainButton("分享報告") { share(scanReport) },
        ))
        scanSummary = note("按「全車掃描」讀取所有模組的故障碼，約 1 分鐘。請停車後再按：行駛中會被拒絕。")
        page.addView(scanSummary)
        scanList = column()
        page.addView(scroll(scanList), LinearLayout.LayoutParams(MATCH_PARENT, 0, 1f))
        return page
    }

    private fun livePage(): View {
        val page = column()
        page.addView(buttonRow(actionButton("開始") { live() }))
        liveNote = note("只讀引擎，行駛中也可以用。按上方「停止」結束。")
        page.addView(liveNote)
        liveGrid = GridLayout(this).apply { columnCount = 2 }
        page.addView(scroll(liveGrid), LinearLayout.LayoutParams(MATCH_PARENT, 0, 1f))
        return page
    }

    private fun batteryPage(): View {
        val page = column()
        batteryText = mono()
        page.addView(buttonRow(
            actionButton("電瓶檢查") { textJob("battery", batteryText) },
            actionButton("啟動壓降測試") {
                AlertDialog.Builder(this).setTitle("啟動壓降測試")
                    .setMessage("先熄火，點火開關轉到 ON (不發動)。\n按「開始」後畫面出現提示時，再轉鑰匙發動。全程約 30 秒。")
                    .setPositiveButton("開始") { _, _ -> textJob("battery --crank 30", batteryText) }
                    .setNegativeButton("取消", null).show()
            },
        ))
        page.addView(note("熄火時估計電量，發動時檢查充電電壓。"))
        page.addView(scroll(batteryText), LinearLayout.LayoutParams(MATCH_PARENT, 0, 1f))
        return page
    }

    private fun featurePage(): View {
        val page = column()
        page.addView(buttonRow(actionButton("讀取目前設定") { loadFeatures() }))
        featureNote = note("09 中央電子的隱藏功能。按右邊的按鈕切換：會先讀車上的值、顯示新舊值，你按「寫入」才送出。")
        page.addView(featureNote)
        featureList = column()
        page.addView(scroll(featureList), LinearLayout.LayoutParams(MATCH_PARENT, 0, 1f))
        return page
    }

    private fun servicePage(): View {
        val page = column()
        page.addView(buttonRow(
            actionButton("讀取里程") { readOdometer() },
            plainButton("記錄換機油") { recordOilChange() },
        ))
        page.addView(note("換機油提醒只記在手機裡，不會寫入車子；儀表板自己的保養提醒不受影響。全車掃描時也會順便更新里程。"))
        serviceText = TextView(this).apply { textSize = 16f; setPadding(px(4), px(8), px(4), px(8)) }
        page.addView(scroll(serviceText), LinearLayout.LayoutParams(MATCH_PARENT, 0, 1f))
        worker.execute {
            val json = py().callAttr("service_status").toString()
            runOnUiThread { showService(json) }
        }
        return page
    }

    private fun advancedPage(): View {
        val page = column()
        val quick = listOf("電壓" to "voltage", "引擎 OBD" to "obd", "失火" to "misfire", "排放" to "readiness", "VCDS 比對" to "verify")
        val quickRow = LinearLayout(this)
        for ((label, cmd) in quick) {
            quickRow.addView(actionButton(label) { textJob(cmd) }.apply { setPadding(0, 0, 0, 0); textSize = 12f },
                LinearLayout.LayoutParams(0, WRAP_CONTENT, 1f))
        }
        page.addView(quickRow)
        argsField = EditText(this).apply { hint = "讀取指令，例如 kwp-dtc 09"; isSingleLine = true }
        page.addView(row(argsField, actionButton("執行") { textJob(argsField.text.toString()) }))
        page.addView(buttonRow(
            plainButton("清除畫面") { log.text = "" },
            plainButton("分享記錄") { share(log.text.toString()) },
        ))
        log = mono()
        logScroll = scroll(log)
        page.addView(logScroll, LinearLayout.LayoutParams(MATCH_PARENT, 0, 1f))
        return page
    }

    private fun column() = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }

    private fun scroll(child: View) = ScrollView(this).apply { addView(child) }

    private fun row(main: View, side: View) = LinearLayout(this).apply {
        addView(main, LinearLayout.LayoutParams(0, WRAP_CONTENT, 1f))
        addView(side, LinearLayout.LayoutParams(WRAP_CONTENT, WRAP_CONTENT))
    }

    private fun buttonRow(vararg buttons: Button) = LinearLayout(this).apply {
        buttons.forEach { addView(it, LinearLayout.LayoutParams(0, WRAP_CONTENT, 1f)) }
    }

    private fun note(text: String) = TextView(this).apply {
        this.text = text
        textSize = 13f
        setPadding(px(4), px(4), px(4), px(8))
    }

    private fun mono() = TextView(this).apply {
        typeface = Typeface.MONOSPACE
        textSize = 12f
        setTextIsSelectable(true)
    }

    private fun plainButton(label: String, onClick: () -> Unit) = Button(this).apply {
        text = label
        isAllCaps = false
        setOnClickListener { onClick() }
    }

    /** A button that starts a car command: disabled while one runs or without a link. */
    private fun actionButton(label: String, onClick: () -> Unit) = plainButton(label, onClick).also { actionButtons += it }

    private fun showPage(index: Int) {
        pages.forEachIndexed { i, p -> p.visibility = if (i == index) View.VISIBLE else View.GONE }
        tabs.forEachIndexed { i, b ->
            b.alpha = if (i == index) 1f else 0.55f
            b.typeface = if (i == index) Typeface.DEFAULT_BOLD else Typeface.DEFAULT
        }
    }

    private fun appendLog(text: String) {
        log.append(text)
        logScroll.post { logScroll.fullScroll(View.FOCUS_DOWN) }
    }

    private fun toast(text: String) = Toast.makeText(this, text, Toast.LENGTH_SHORT).show()

    private fun dialog(title: String, message: String) {
        AlertDialog.Builder(this).setTitle(title).setMessage(message).setPositiveButton("好", null).show()
    }

    private fun share(text: String) {
        if (text.isBlank()) return toast("還沒有內容可以分享")
        val send = Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_TEXT, text)
        startActivity(Intent.createChooser(send, "分享"))
    }

    private fun setBusy(running: Boolean, stoppable: Boolean = true) {
        busy = running
        val ready = !running && link?.closed == false
        (actionButtons + listButtons).forEach { it.isEnabled = ready }
        connectButton.isEnabled = !running
        stopButton.visibility = if (running && stoppable) View.VISIBLE else View.INVISIBLE
    }

    // ---------------------------------------------------------------- Bluetooth

    @SuppressLint("MissingPermission")
    private fun loadDevices() {
        val adapter = getSystemService(BluetoothManager::class.java)?.adapter
        if (adapter == null || !adapter.isEnabled) {
            status.text = "藍牙沒有開啟。開啟後回到 App 就會列出接頭。"
            return
        }
        devices = adapter.bondedDevices.sortedBy { if (isObdName(it.name)) 0 else 1 }
        deviceSpinner.adapter = ArrayAdapter(this, android.R.layout.simple_spinner_dropdown_item,
            devices.map { "${it.name ?: "(無名稱)"}  ${it.address}" })
        val last = getPreferences(MODE_PRIVATE).getString("device", null)
        devices.indexOfFirst { it.address == last }.takeIf { it >= 0 }?.let { deviceSpinner.setSelection(it) }
        status.text = if (devices.isEmpty()) "沒有已配對的藍牙裝置。先到手機藍牙設定配對接頭 (密碼通常 1234 或 0000)。" else connectedText
    }

    private fun isObdName(name: String?) =
        name != null && Regex("obd|elm|v-?link|vgate|icar|konnwei|viecar|veepeak", RegexOption.IGNORE_CASE)
            .containsMatchIn(name)

    @SuppressLint("MissingPermission")
    private fun connect() {
        val device = devices.getOrNull(deviceSpinner.selectedItemPosition) ?: return toast("先選一個接頭")
        getPreferences(MODE_PRIVATE).edit().putString("device", device.address).apply()
        status.text = "連線中：${device.name} …"
        setBusy(true, stoppable = false)
        worker.execute {
            link?.close()
            link = null
            val result = try {
                link = BtLink.connect(device) { runOnUiThread { appendLog(it) } }
                "已連線：${device.name}"
            } catch (e: Exception) {
                "連線失敗：${e.message}"
            }
            runOnUiThread {
                connectedText = if (link != null) result else "未連線"
                status.text = result
                appendLog("$result\n")
                setBusy(false)
            }
        }
    }

    private fun stop() {
        liveStop.set(true)             // the live loop ends after the value it is reading
        if (!liveRunning) link?.abort() // other reads: the next Bluetooth read / write fails
        status.text = "停止中 …"
    }

    // ---------------------------------------------------------------- running commands

    /**
     * Run `work` on the worker thread with the open link; `done` gets its result on the UI thread.
     * The status line shows the elapsed seconds and the last line the command printed.
     */
    private fun job(label: String, stoppable: Boolean = true, sink: Sink = Sink(),
                    work: (BtLink, Sink) -> String, done: (String) -> Unit) {
        val l = link
        if (l == null || l.closed) return toast("先選接頭按「連線」")
        if (busy) return
        l.resume()
        liveStop.set(false)
        progress = ""
        setBusy(true, stoppable)
        appendLog("\n> $label\n")
        val started = System.currentTimeMillis()
        val tick = object : Runnable {
            override fun run() {
                val secs = (System.currentTimeMillis() - started) / 1000
                status.text = "$label … $secs 秒" + if (progress.isEmpty()) "" else "\n$progress"
                status.postDelayed(this, 1000)
            }
        }
        tick.run()
        worker.execute {
            val result = try {
                work(l, sink)
            } catch (e: Exception) {
                JSONObject().put("ok", false).put("message", e.message ?: e.toString()).toString()
            }
            runOnUiThread {
                status.removeCallbacks(tick)
                if (l.closed) connectedText = "連線中斷，請重新連線"
                status.text = connectedText
                setBusy(false)
                done(result)
            }
        }
    }

    /** A read command with text output; shown in the log and, if given, on a page. */
    private fun textJob(args: String, mirror: TextView? = null) {
        if (args.isBlank()) return
        mirror?.text = ""
        job(args, sink = Sink(mirror), work = { l, s -> py().callAttr("run", l, args, s).toInt().toString() }) { code ->
            appendLog("[結束碼 $code]\n")
        }
    }

    /**
     * A write: plan (all checks, nothing written) -> dialog with old and new -> apply, once.
     * The stop button is hidden while the write itself runs.
     */
    private fun askWrite(label: String, command: String, after: () -> Unit) {
        job("檢查：$label", work = { l, s -> py().callAttr("plan", l, command, s).toString() }) { json ->
            val plan = JSONObject(json)
            if (!plan.optBoolean("ok")) return@job dialog("不能寫入", plan.optString("message"))
            if (plan.isNull("plan_id")) return@job dialog(plan.optString("title"), plan.optString("message"))
            val id = plan.getString("plan_id")
            AlertDialog.Builder(this).setTitle(plan.getString("title")).setMessage(plan.getString("message"))
                .setNegativeButton("取消", null)
                .setPositiveButton("寫入") { _, _ ->
                    job("寫入：$label", stoppable = false,
                        work = { l, s -> py().callAttr("apply", l, id, s).toString() }) { result ->
                        val r = JSONObject(result)
                        dialog(if (r.optBoolean("ok")) "完成" else "寫入沒有成功", r.optString("message"))
                        after()
                    }
                }.show()
        }
    }

    // ---------------------------------------------------------------- pages

    private fun scan() {
        job("全車掃描", work = { l, s -> py().callAttr("scan", l, s).toString() }) { json ->
            val r = JSONObject(json)
            listButtons.clear()
            scanList.removeAllViews()
            if (!r.optBoolean("ok")) {
                scanSummary.text = "掃描失敗：${r.optString("message")}"
                return@job
            }
            scanReport = r.optString("report")
            scanSummary.text = r.getString("summary")
            val modules = r.getJSONArray("modules")
            val faulty = (0 until modules.length()).map { modules.getJSONObject(it) }
                .filter { it.optBoolean("clearable") }.map { it.getString("address") }
            if (faulty.size > 1) {
                val all = plainButton("清除全部故障碼（${faulty.size} 個模組）") {
                    askWrite("清除全部故障碼", "clear-dtc " + faulty.joinToString(" ")) {
                        scanSummary.text = "已處理。按「全車掃描」重新確認哪些故障還在。"
                    }
                }
                listButtons += all
                scanList.addView(all)
            }
            for (i in 0 until modules.length()) addModule(modules.getJSONObject(i))
            setBusy(false)
        }
    }

    private fun addModule(m: JSONObject) {
        val address = m.getString("address")
        val name = m.getString("name")
        val count = if (m.isNull("count")) -1 else m.getInt("count")
        val head = when {
            count < 0 -> "無法讀取"
            count == 0 -> "正常"
            else -> "故障碼 $count 個"
        }
        scanList.addView(TextView(this).apply {
            text = "$address $name　$head"
            textSize = 15f
            typeface = if (count != 0) Typeface.DEFAULT_BOLD else Typeface.DEFAULT
            if (count != 0) setTextColor(0xFFFFB74D.toInt())
            setPadding(px(4), px(10), px(4), 0)
        })
        val lines = mutableListOf("零件 ${m.optString("part")}")
        if (!m.isNull("error")) lines += "讀取問題: ${m.getString("error")}"
        val dtcs = m.getJSONArray("dtcs")
        for (i in 0 until dtcs.length()) lines += "● ${dtcs.getString(i)}"
        val values = m.getJSONArray("values")
        for (i in 0 until values.length()) lines += values.getString(i)
        scanList.addView(TextView(this).apply {
            text = lines.joinToString("\n")
            textSize = 12f
            setTextIsSelectable(true)
            setPadding(px(12), 0, px(4), 0)
        })
        if (count > 0 && m.optBoolean("clearable")) {
            val clear = plainButton("清除 $address 的故障碼") {
                askWrite("清除 $address $name 故障碼", "clear-dtc $address") {
                    scanSummary.text = "已處理 $address $name。按「全車掃描」重新確認故障還在不在。"
                }
            }
            listButtons += clear
            scanList.addView(clear, LinearLayout.LayoutParams(WRAP_CONTENT, WRAP_CONTENT).apply { leftMargin = px(8) })
        }
    }

    private fun live() {
        liveTiles.clear()
        liveGrid.removeAllViews()
        liveRunning = true
        job("即時數據", work = { l, s -> py().callAttr("live", l, s, liveStop).toString() }) { json ->
            liveRunning = false
            val r = JSONObject(json)
            liveNote.text = if (r.optBoolean("ok")) "已停止。" else "已停止：${r.optString("message")}"
        }
        if (!busy) liveRunning = false  // no link: the job did not start
    }

    private fun showLive(values: JSONArray) {
        liveNote.text = "更新中 …"
        for (i in 0 until values.length()) {
            val v = values.getJSONObject(i)
            val name = v.getString("name")
            val tile = liveTiles.getOrPut(name) {
                val number = TextView(this).apply { textSize = 34f; typeface = Typeface.DEFAULT_BOLD }
                val box = column().apply {
                    setPadding(px(12), px(10), px(12), px(10))
                    addView(TextView(this@MainActivity).apply { text = "$name (${v.getString("unit")})"; textSize = 13f })
                    addView(number)
                }
                liveGrid.addView(box, GridLayout.LayoutParams().apply {
                    width = 0
                    columnSpec = GridLayout.spec(GridLayout.UNDEFINED, 1f)
                })
                number
            }
            tile.text = v.getString("value")
        }
    }

    private fun showService(json: String) {
        val r = JSONObject(json)
        if (!r.optBoolean("ok")) return dialog("保養", r.optString("message"))
        serviceState = r
        serviceText.text = r.optString("message")
        when (r.optString("level")) {
            "over" -> serviceText.setTextColor(0xFFE57373.toInt())
            "soon" -> serviceText.setTextColor(0xFFFFB74D.toInt())
            else -> serviceText.setTextColor(status.currentTextColor)
        }
    }

    private fun readOdometer() {
        job("讀取里程", work = { l, s -> py().callAttr("service_read", l, s).toString() }) { showService(it) }
    }

    /** Phone-only record: the odometer at the oil change (editable) and the interval to the next one. */
    private fun recordOilChange() {
        val km = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_NUMBER
            hint = "換機油時的里程 (km)"
            if (!serviceState.isNull("odometer")) setText(serviceState.optInt("odometer").toString())
        }
        val choices = RadioGroup(this)
        val intervals = serviceState.optJSONArray("intervals") ?: JSONArray(listOf(5000, 7500, 10000))
        val last = serviceState.optJSONObject("oil")?.optInt("interval") ?: 7500
        for (i in 0 until intervals.length()) {
            val v = intervals.getInt(i)
            choices.addView(RadioButton(this).apply { text = "$v km 後換"; id = v; isChecked = v == last })
        }
        val body = column().apply {
            setPadding(px(20), px(8), px(20), 0)
            addView(TextView(this@MainActivity).apply { text = "換機油時的里程 (可以改成之前換的里程)" })
            addView(km)
            addView(TextView(this@MainActivity).apply { text = "下一次多久後換"; setPadding(0, px(8), 0, 0) })
            addView(choices)
        }
        AlertDialog.Builder(this).setTitle("記錄換機油").setView(body)
            .setNegativeButton("取消", null)
            .setPositiveButton("記錄") { _, _ ->
                val odometer = km.text.toString()
                val interval = choices.checkedRadioButtonId
                worker.execute {
                    val json = py().callAttr("service_done", odometer, interval).toString()
                    runOnUiThread { showService(json) }
                }
            }.show()
    }

    private fun loadFeatures() {
        job("讀取隱藏功能", work = { l, s -> py().callAttr("features", l, s).toString() }) { json ->
            val r = JSONObject(json)
            listButtons.clear()
            featureList.removeAllViews()
            if (!r.optBoolean("ok")) {
                featureNote.text = "讀取失敗：${r.optString("message")}"
                return@job
            }
            featureNote.text = "按右邊的按鈕切換。會先顯示新舊值，你按「寫入」才送出。\n長編碼 ${r.getString("coding")}"
            val list = r.getJSONArray("features")
            for (i in 0 until list.length()) addFeature(list.getJSONObject(i))
            setBusy(false)
        }
    }

    private fun addFeature(f: JSONObject) {
        val on = f.getBoolean("on")
        val name = f.getString("name")
        val proven = f.optString("verified") == "實車寫入"
        val text = TextView(this).apply {
            text = name + "\n" + f.optString("note") + if (proven) "" else "\n(位置來自 VCDS 說明，還沒在本車寫過)"
            textSize = 13f
            setPadding(px(4), px(8), px(8), px(8))
        }
        val toggle = plainButton(if (on) "開" else "關") {
            val target = if (on) "關" else "開"
            askWrite("$name → $target", "feature ${f.getString("key")} ${if (on) "off" else "on"}") { loadFeatures() }
        }.apply { if (on) setTextColor(0xFF81C784.toInt()) }
        listButtons += toggle
        featureList.addView(row(text, toggle).apply { gravity = Gravity.CENTER_VERTICAL })
    }
}
