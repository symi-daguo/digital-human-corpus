import java.io.*;
import java.lang.reflect.*;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.file.Files;

public class VadRel {
    static void p(String s){ System.out.println(s); System.out.flush(); }
    static float[] readWav(String path) throws Exception {
        byte[] b = Files.readAllBytes(new File(path).toPath());
        int off = 12, dataOff = -1, dataLen = -1;
        while (off + 8 <= b.length) {
            String id = new String(b, off, 4, "US-ASCII");
            int len = ByteBuffer.wrap(b, off + 4, 4).order(ByteOrder.LITTLE_ENDIAN).getInt();
            if (id.equals("data")) { dataOff = off + 8; dataLen = len; break; }
            off += 8 + len + (len % 2);
        }
        int n = Math.min(dataLen, b.length - dataOff) / 2;
        float[] out = new float[n];
        ByteBuffer bb = ByteBuffer.wrap(b, dataOff, n * 2).order(ByteOrder.LITTLE_ENDIAN);
        for (int i = 0; i < n; i++) out[i] = bb.getShort() / 32768f;
        return out;
    }

    public static void main(String[] a) throws Exception {
        System.loadLibrary("onnxruntime");
        System.loadLibrary("sherpa-onnx-c-api");
        System.loadLibrary("sherpa-onnx-cxx-api");
        System.loadLibrary("sherpa-onnx-jni");
        String apk = a[0], wav = a[1], appCls = a[2];

        Class<?> amC = Class.forName("android.content.res.AssetManager");
        Constructor<?> c0 = amC.getDeclaredConstructor(); c0.setAccessible(true);
        Object am = c0.newInstance();
        Method add = amC.getDeclaredMethod("addAssetPath", String.class); add.setAccessible(true);
        add.invoke(am, apk);
        p("[1] AssetManager 注入 APK 完成");

        Class<?> shell = Class.forName(appCls);
        Object wrapper = shell.getDeclaredConstructors()[0].newInstance(am);
        p("[2] 实例化 " + appCls + " 成功（配置与 ONNX 会话由 release 代码建立）");

        Class<?> vadC = Class.forName("com.k2fsa.sherpa.onnx.Vad");
        Field fd = null;
        for (Field f : shell.getDeclaredFields()) {
            if (vadC.isAssignableFrom(f.getType())) { fd = f; break; }
        }
        if (fd == null) { p("[3] FAIL 未找到 Vad 字段"); return; }
        fd.setAccessible(true);
        Object vad = fd.get(wrapper);
        p("[3] 取到底层 Vad 实例 = " + vad.getClass().getName() + "（R8 保留，未被混淆）");

        Method accept = vadC.getMethod("acceptWaveform", float[].class);
        Method flush = vadC.getMethod("flush");
        Method empty = vadC.getMethod("empty");
        Method front = vadC.getMethod("front");
        Method pop = vadC.getMethod("pop");

        float[] pcm = readWav(wav);
        p("[4] 读入 " + pcm.length + " 采样 (" + String.format("%.2f", pcm.length / 16000.0) + "s)");

        long t = System.currentTimeMillis();
        for (int i = 0; i < pcm.length; i += 1600) {
            int len = Math.min(1600, pcm.length - i);
            float[] c = new float[len];
            System.arraycopy(pcm, i, c, 0, len);
            accept.invoke(vad, (Object) c);
        }
        flush.invoke(vad);
        long cost = System.currentTimeMillis() - t;
        p("[5] 推理完成 耗时=" + cost + "ms  实时倍率=" + String.format("%.1f", (pcm.length/16000.0)/(cost/1000.0)) + "x");

        int n = 0;
        while (!(Boolean) empty.invoke(vad)) {
            Object seg = front.invoke(vad);
            float[] s = (float[]) seg.getClass().getMethod("getSamples").invoke(seg);
            int start = (Integer) seg.getClass().getMethod("getStart").invoke(seg);
            p("[6] SEG#" + n + " start=" + start + " 时长=" + String.format("%.2f", s.length/16000.0) + "s");
            pop.invoke(vad); n++;
            if (n > 30) break;
        }
        p("[7] TOTAL_SEGMENTS=" + n);
        p("[8] RESULT=" + (n > 0 ? "PASS 混淆后 VAD 链路完好" : "FAIL 未检出语音段"));
    }
}
