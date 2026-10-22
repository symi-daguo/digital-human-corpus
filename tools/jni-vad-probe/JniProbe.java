public class JniProbe {
    public static void main(String[] args) {
        try {
            System.loadLibrary("onnxruntime");
            System.out.println("OK load onnxruntime");
        } catch (Throwable t) { System.out.println("FAIL onnxruntime: " + t); }
        try {
            System.loadLibrary("sherpa-onnx-c-api");
            System.out.println("OK load sherpa-onnx-c-api");
        } catch (Throwable t) { System.out.println("FAIL sherpa-onnx-c-api: " + t); }
        try {
            System.loadLibrary("sherpa-onnx-cxx-api");
            System.out.println("OK load sherpa-onnx-cxx-api");
        } catch (Throwable t) { System.out.println("FAIL sherpa-onnx-cxx-api: " + t); }
        try {
            System.loadLibrary("sherpa-onnx-jni");
            System.out.println("OK load sherpa-onnx-jni");
        } catch (Throwable t) { System.out.println("FAIL sherpa-onnx-jni: " + t); }
        try {
            Class<?> c = Class.forName("com.k2fsa.sherpa.onnx.Vad");
            System.out.println("OK class " + c.getName());
            for (java.lang.reflect.Method m : c.getDeclaredMethods()) {
                System.out.println("   m: " + m.getName());
            }
        } catch (Throwable t) { System.out.println("FAIL class Vad: " + t); }
        try {
            Class<?> c = Class.forName("com.k2fsa.sherpa.onnx.FeatureConfig");
            System.out.println("OK class " + c.getName());
        } catch (Throwable t) { System.out.println("FAIL class FeatureConfig: " + t); }
    }
}
