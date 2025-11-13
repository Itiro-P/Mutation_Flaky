import org.apache.commons.cli.*;
import com.github.javaparser.StaticJavaParser;
import com.github.javaparser.ast.body.ClassOrInterfaceDeclaration;
import com.github.javaparser.ast.body.MethodDeclaration;
import com.github.javaparser.ast.CompilationUnit;
import com.google.gson.GsonBuilder;

import java.io.File;
import java.util.*;

public class MCParser {
    private Map<Integer, Integer> changedLines;
    private Map<String, MethodInfo> methodMap = new HashMap<>();
    private Map<String, MethodInfo> allMethodsMap = new HashMap<>();
    private Map<String, Set<String>> callGraph = new HashMap<>();
    private Map<String, Set<String>> reverseCallGraph = new HashMap<>();

    public static void main(String[] args) throws Exception {
        Options opts = new Options();
        opts.addOption("f", "file", true, "File to parse.");
        opts.addOption("l", "lines", true, "Lines to check.");
        opts.addOption("h", "help", false, "Print this help and exit.");

        CommandLineParser cliParser = new DefaultParser();
        try {
            CommandLine cmd = cliParser.parse(opts, args);
            if (cmd.hasOption("h")) {
                printHelp(opts);
                return;
            }
            if (!cmd.hasOption("f") || !cmd.hasOption("l")) {
                System.out.println("Error: arguments -f and -l are mandatory.");
                printHelp(opts);
                return;
            }

            MCParser parser = new MCParser();
            parser.analyze(cmd.getOptionValue("f"), cmd.getOptionValues("l"));
        } catch (ParseException e) {
            System.out.println("Error parsing arguments: " + e.getMessage());
            e.printStackTrace();
        }
    }

    private void analyze(String filePath, String[] lineArgs) throws Exception {
        changedLines = getLines(lineArgs);
        CompilationUnit cu = StaticJavaParser.parse(new File(filePath));

        buildMethodMap(cu);
        buildAllMethodsMap(cu);
        buildCallGraph(cu);

        Set<String> affected = methodMap.keySet();
        Set<String> callers = findCallers(affected);
        Set<String> callees = findCallees(affected);

        Map<String, Object> result = new LinkedHashMap<>();
        result.put("affectedMethods", new ArrayList<>(affected));
        result.put("callers", new ArrayList<>(callers));
        result.put("callees", new ArrayList<>(callees));

        System.out.println(new GsonBuilder().setPrettyPrinting().create().toJson(result));
    }

    private void buildMethodMap(CompilationUnit cu) {
        cu.findAll(ClassOrInterfaceDeclaration.class).forEach(cls -> {
            String className = cls.getFullyQualifiedName().orElse(cls.getNameAsString());
            cls.getMethods().forEach(m -> {
                int start = m.getBegin().map(n -> n.line).orElse(0);
                int end = m.getEnd().map(n -> n.line).orElse(0);
                
                System.err.println("[DEBUG-METHODS] Found method: " + className + "." + m.getNameAsString() + " [" + start + "-" + end + "]");
                
                if (isLineInRange(start, end)) {
                    System.err.println("  ✓ IN RANGE - Added to affected");
                    methodMap.put(className + "." + m.getSignature(),
                            new MethodInfo(className, m.getNameAsString(), start, end));
                } else {
                    System.err.println("  ✗ OUT OF RANGE");
                }
            });
        });
    }

    private void buildAllMethodsMap(CompilationUnit cu) {
        cu.findAll(ClassOrInterfaceDeclaration.class).forEach(cls -> {
            String className = cls.getFullyQualifiedName().orElse(cls.getNameAsString());
            cls.getMethods().forEach(m -> {
                int start = m.getBegin().map(n -> n.line).orElse(0);
                int end = m.getEnd().map(n -> n.line).orElse(0);
                allMethodsMap.put(className + "." + m.getSignature(),
                        new MethodInfo(className, m.getNameAsString(), start, end));
            });
        });
    }

    private void buildCallGraph(CompilationUnit cu) {
        Set<String> affected = methodMap.keySet();
        
        if (affected.isEmpty()) {
            System.err.println("[DEBUG] No affected methods, skipping call graph");
            return;
        }
        
        System.err.println("[DEBUG] Building call graph for " + affected.size() + " methods");

        // Find callees: Methods called by affected methods
        affected.forEach(affectedMethod -> {
            cu.findAll(MethodDeclaration.class).stream()
                .filter(m -> getMethodKey(m).equals(affectedMethod))
                .findFirst()
                .ifPresent(m -> {
                    List<com.github.javaparser.ast.expr.MethodCallExpr> calls = m.findAll(com.github.javaparser.ast.expr.MethodCallExpr.class);
                    calls.forEach(call -> {
                        String calledName = call.getNameAsString();
                        allMethodsMap.keySet().stream()
                            .filter(k -> k.endsWith("." + calledName))
                            .forEach(calledMethod -> {
                                callGraph.putIfAbsent(affectedMethod, new HashSet<>()).add(calledMethod);
                            });
                    });
                });
        });

        // Find callers: Methods that call affected methods
        List<MethodDeclaration> allMethods = cu.findAll(MethodDeclaration.class);
        affected.forEach(affectedMethod -> {
            allMethods.stream()
                .filter(m -> {
                    List<com.github.javaparser.ast.expr.MethodCallExpr> calls = m.findAll(com.github.javaparser.ast.expr.MethodCallExpr.class);
                    return calls.stream()
                        .anyMatch(call -> affectedMethod.endsWith("." + call.getNameAsString()));
                })
                .forEach(caller -> {
                    reverseCallGraph.putIfAbsent(affectedMethod, new HashSet<>()).add(getMethodKey(caller));
                });
        });
        
        System.err.println("[DEBUG] Call graph built. Callees: " + callGraph.size() + ", Callers: " + reverseCallGraph.size());
    }

    private String getMethodKey(MethodDeclaration m) {
        return m.getParentNode()
            .filter(ClassOrInterfaceDeclaration.class::isInstance)
            .map(ClassOrInterfaceDeclaration.class::cast)
            .map(c -> c.getFullyQualifiedName().orElse(c.getNameAsString()))
            .map(cn -> cn + "." + m.getSignature())
            .orElse("");
    }

    private boolean isLineInRange(int start, int end) {
        System.err.println("[DEBUG-RANGE] Checking if [" + start + "-" + end + "] intersects with changedLines: " + changedLines);
        boolean result = changedLines.values().stream()
            .anyMatch(maxLine -> changedLines.entrySet().stream()
                .anyMatch(e -> start <= maxLine && start >= e.getKey()));
        System.err.println("  Result: " + result);
        return result;
    }

    private Set<String> findCallers(Set<String> affected) {
        Set<String> result = new HashSet<>();
        affected.forEach(m -> result.addAll(reverseCallGraph.getOrDefault(m, new HashSet<>())));
        return result;
    }

    private Set<String> findCallees(Set<String> affected) {
        Set<String> result = new HashSet<>();
        affected.forEach(m -> result.addAll(callGraph.getOrDefault(m, new HashSet<>())));
        return result;
    }

    private static Map<Integer, Integer> getLines(final String[] lines) {
        Map<Integer, Integer> lineRange = new HashMap<>();
        for (String line : lines) {
            int pos = line.indexOf("-");
            if (pos != -1) {
                int first = Integer.parseInt(line.substring(0, pos).trim());
                int second = Integer.parseInt(line.substring(pos + 1).trim());
                lineRange.put(first, second);
            } else {
                int num = Integer.parseInt(line.trim());
                lineRange.put(num, num);
            }
        }
        return lineRange;
    }

    private static void printHelp(Options options) {
        HelpFormatter formatter = new HelpFormatter();
        formatter.printHelp("java MCParser", options);
    }

    private static class MethodInfo {
        String className, methodName;
        int startLine, endLine;

        MethodInfo(String className, String methodName, int startLine, int endLine) {
            this.className = className;
            this.methodName = methodName;
            this.startLine = startLine;
            this.endLine = endLine;
        }
    }
}