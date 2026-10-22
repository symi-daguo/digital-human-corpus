package com.symi.digitalhuman.data

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.room.migration.Migration
import androidx.sqlite.db.SupportSQLiteDatabase

@Database(
    entities = [Session::class, Segment::class, DailyReport::class, StatHour::class, ServiceEvent::class],
    version = 3,
    exportSchema = false,
)
abstract class AppDatabase : RoomDatabase() {
    abstract fun sessionDao(): SessionDao
    abstract fun segmentDao(): SegmentDao
    abstract fun reportDao(): ReportDao
    abstract fun statDao(): StatDao
    abstract fun serviceEventDao(): ServiceEventDao

    companion object {
        @Volatile private var inst: AppDatabase? = null

        /**
         * v1 → v2：为 segment 补说话人回流字段（speakerLabel / isSpeech）+ 高频查询索引。
         *
         * 必须显式提供迁移。此前用的是 fallbackToDestructiveMigration()，
         * 它会在任何版本变更时**静默清空整库**——session/segment/report/stat 全丢。
         * 只要还需要升版本（本次加字段就是），就不能留它：宁可漏迁移时崩溃报错，
         * 也不能让用户的历史录音记录无声无息消失。
         */
        private val MIGRATION_1_2 = object : Migration(1, 2) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL(
                    "ALTER TABLE segment ADD COLUMN speakerLabel TEXT NOT NULL DEFAULT ''"
                )
                db.execSQL(
                    "ALTER TABLE segment ADD COLUMN isSpeech INTEGER DEFAULT NULL"
                )
                // 索引名必须与 Room 为 @Index 自动生成的命名一致，否则校验失败：
                // index_<表名>_<列名>
                db.execSQL("CREATE INDEX IF NOT EXISTS `index_segment_sessionId` ON `segment` (`sessionId`)")
                db.execSQL("CREATE INDEX IF NOT EXISTS `index_segment_status` ON `segment` (`status`)")
                db.execSQL("CREATE INDEX IF NOT EXISTS `index_segment_startOffsetMs` ON `segment` (`startOffsetMs`)")
            }
        }

        /**
         * v2 → v3：为 daily_report 补「总结卡片」字段（Mac 端按栏目解析后的结构化总结）。
         *
         * 同样是显式迁移：历史日报不能因为加字段被清空。默认值空串表示
         * 「没有卡片」，UI 回退展示整段总结文本——老数据无需回填也能正常显示。
         */
        private val MIGRATION_2_3 = object : Migration(2, 3) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL(
                    "ALTER TABLE daily_report ADD COLUMN summaryCards TEXT NOT NULL DEFAULT ''"
                )
            }
        }

        fun get(ctx: Context): AppDatabase = inst ?: synchronized(this) {
            inst ?: Room.databaseBuilder(
                ctx.applicationContext, AppDatabase::class.java, "digital-human.db"
            )
                .addMigrations(MIGRATION_1_2, MIGRATION_2_3)
                // 仅在降级（装回旧版）时重建：降级无法自动迁移，且旧版本读不懂新列。
                // 参数 true = 降级时清空全部表（无参重载已废弃）。
                .fallbackToDestructiveMigrationOnDowngrade(true)
                .build().also { inst = it }
        }
    }
}
