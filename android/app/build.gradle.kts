plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

android {
    namespace = "io.github.ilshang2.yetidiag"
    compileSdk = 35

    defaultConfig {
        applicationId = "io.github.ilshang2.yetidiag"
        minSdk = 24
        targetSdk = 35
        versionCode = 4
        versionName = "0.4"
        ndk { abiFilters += listOf("arm64-v8a") }  // Python 3.12+ is 64-bit only
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

kotlin {
    compilerOptions { jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17) }
}

// The Python core is shared with the PC version: copy yeti_diag/ and profiles/ from the
// project root instead of keeping a second copy. The TP2.0 address map probed on the PC
// (reports/, not in git) is bundled as a first-run seed when it exists.
val projectRoot = rootDir.parentFile
val stagePython by tasks.registering(Sync::class) {
    from(projectRoot) {
        include("yeti_diag/**", "profiles/**")
        exclude("**/__pycache__/**")
    }
    from(File(projectRoot, "reports")) {
        include("tp20_map.json")
        into("yeti_seed")
    }
    into(layout.buildDirectory.dir("pysrc"))
}
tasks.named("preBuild") { dependsOn(stagePython) }
tasks.matching { it.name.startsWith("merge") && it.name.endsWith("PythonSources") }
    .configureEach { dependsOn(stagePython) }

chaquopy {
    defaultConfig {
        version = "3.13"
        pip {
            install("python-can==4.6.1")
            install("can-isotp==2.0.7")
        }
    }
    sourceSets {
        getByName("main") { srcDir("build/pysrc") }
    }
}
