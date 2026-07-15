import java.io.File;
import java.io.FileOutputStream;
import java.lang.reflect.Method;
import java.util.List;

public class Main {
    public static void main(String[] args) {
        System.out.println("Starting App Icon Extractor...");

        try {
            // FIX: Initialize the message looper for the background terminal thread
            Class<?> looperClass = Class.forName("android.os.Looper");
            Method prepareMethod = looperClass.getMethod("prepare");
            prepareMethod.invoke(null);

            // 1. Get System Context entirely through Reflection
            Class<?> activityThreadClass = Class.forName("android.app.ActivityThread");
            Method systemMainMethod = activityThreadClass.getMethod("systemMain");
            Object activityThread = systemMainMethod.invoke(null);

            Method getSystemContextMethod = activityThreadClass.getMethod("getSystemContext");
            Object context = getSystemContextMethod.invoke(activityThread);

            // 2. Get PackageManager
            Method getPackageManagerMethod = context.getClass().getMethod("getPackageManager");
            Object pm = getPackageManagerMethod.invoke(context);

            // 3. Query all installed packages (128 = PackageManager.GET_META_DATA)
            Method getInstalledApplicationsMethod = pm.getClass().getMethod("getInstalledApplications", int.class);
            List<?> apps = (List<?>) getInstalledApplicationsMethod.invoke(pm, 128);

            File outputDir = new File("/data/local/tmp/ExtractedIcons");
            if (!outputDir.exists()) outputDir.mkdirs();

            System.out.println("Found " + apps.size() + " apps. Extracting icons...");

            // Reflective lookups for Android framework classes
            Class<?> applicationInfoClass = Class.forName("android.content.pm.ApplicationInfo");
            Method loadIconMethod = applicationInfoClass.getMethod("loadIcon", Class.forName("android.content.pm.PackageManager"));

            Class<?> bitmapClass = Class.forName("android.graphics.Bitmap");
            Class<?> bitmapConfigClass = Class.forName("android.graphics.Bitmap$Config");
            Object argb8888 = bitmapConfigClass.getField("ARGB_8888").get(null);
            Method createBitmapMethod = bitmapClass.getMethod("createBitmap", int.class, int.class, bitmapConfigClass);

            Class<?> canvasClass = Class.forName("android.graphics.Canvas");
            java.lang.reflect.Constructor<?> canvasConstructor = canvasClass.getConstructor(bitmapClass);

            Class<?> drawableClass = Class.forName("android.graphics.drawable.Drawable");
            Method setBoundsMethod = drawableClass.getMethod("setBounds", int.class, int.class, int.class, int.class);
            Method drawMethod = drawableClass.getMethod("draw", canvasClass);
            Method getIntrinsicWidthMethod = drawableClass.getMethod("getIntrinsicWidth");
            Method getIntrinsicHeightMethod = drawableClass.getMethod("getIntrinsicHeight");

            Class<?> compressFormatClass = Class.forName("android.graphics.Bitmap$CompressFormat");
            Object pngFormat = compressFormatClass.getField("PNG").get(null);
            Method compressMethod = bitmapClass.getMethod("compress", compressFormatClass, int.class, java.io.OutputStream.class);

            for (Object app : apps) {
                String packageName = (String) applicationInfoClass.getField("packageName").get(app);
                try {
                    // 4. Load the icon Drawable
                    Object icon = loadIconMethod.invoke(app, pm);
                    if (icon == null) continue;

                    // 5. Build and draw onto Bitmap
                    int width = (int) getIntrinsicWidthMethod.invoke(icon);
                    int height = (int) getIntrinsicHeightMethod.invoke(icon);

                    // Fallbacks for layout bounds that evaluate to 0 or negative numbers
                    width = Math.max(width, 128);
                    height = Math.max(height, 128);

                    Object bitmap = createBitmapMethod.invoke(null, width, height, argb8888);
                    Object canvas = canvasConstructor.newInstance(bitmap);

                    setBoundsMethod.invoke(icon, 0, 0, width, height);
                    drawMethod.invoke(icon, canvas);

                    // 6. Write image asset to disk
                    File file = new File(outputDir, packageName + ".png");
                    try (FileOutputStream out = new FileOutputStream(file)) {
                        compressMethod.invoke(bitmap, pngFormat, 100, out);
                    }
                    System.out.println("Extracted: " + packageName);
                } catch (Exception e) {
                    System.err.println("Failed to extract: " + packageName);
                }
            }
            System.out.println("Finished! Icons saved to /data/local/tmp/ExtractedIcons/");
        } catch (Exception e) {
            e.printStackTrace();
        }
    }
}
