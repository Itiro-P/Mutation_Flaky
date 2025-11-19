import org.apache.commons.cli.*;
import com.github.javaparser.*;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.ImportDeclaration;
import com.github.javaparser.ast.body.ClassOrInterfaceDeclaration;
import com.github.javaparser.ast.body.MethodDeclaration;
import com.github.javaparser.ast.expr.AnnotationExpr;
import com.github.javaparser.ast.expr.MethodCallExpr;
import com.github.javaparser.resolution.declarations.ResolvedAnnotationDeclaration;
import com.github.javaparser.resolution.declarations.ResolvedMethodDeclaration;
import com.github.javaparser.symbolsolver.JavaSymbolSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.*;
import com.google.gson.Gson;
import com.google.gson.GsonBuilder;

import java.io.File;
import java.io.IOException;
import java.nio.file.*;
import java.util.*;
import java.util.concurrent.*;
import java.util.stream.Collectors;
import java.util.stream.Stream;

public class MCParser {
    private static final Set<String> EXTERNAL_PREFIXES = Set.of("java.", "javax.", "jdk.", "com.google.");

    private final ConcurrentMap<String, MethodInfo> allMethods = new ConcurrentHashMap<>();
    private final ConcurrentMap<String, Set<String>> callGraph = new ConcurrentHashMap<>();
    private final ConcurrentMap<String, Set<String>> reverseCallGraph = new ConcurrentHashMap<>();
    private final ConcurrentMap<Path, CompilationUnit> compilationUnits = new ConcurrentHashMap<>();

    private final ConcurrentMap<String, String> resolvedMethodCache = new ConcurrentHashMap<>();
    private final ConcurrentMap<String, String> resolvedCallCache = new ConcurrentHashMap<>();

    // cache for class -> isTest
    private final ConcurrentMap<String, Boolean> testClassCache = new ConcurrentHashMap<>();

    private Map<Integer,Integer> changedRanges = new TreeMap<>();
    private boolean quiet = false;
    private boolean includeFound = false;

    private static final Gson GSON = new GsonBuilder().disableHtmlEscaping().setPrettyPrinting().create();

    // heuristics constants
    private static final Set<String> TEST_ANNOT_SIMPLE = Set.of(
            "Test", "ParameterizedTest", "RepeatedTest", "BeforeEach", "AfterEach",
            "BeforeAll", "AfterAll", "RunWith", "ExtendWith", "Factory"
    );
    private static final Set<String> TEST_ANNOT_PKG_PREFIX = Set.of(
            "org.junit", "org.testng", "spock.lang"
    );

    public static void main(String[] args) throws Exception {
        Options opts = new Options();
        opts.addOption("p","path", true, "Project root path");
        opts.addOption("f","file", true, "Target file to analyze (relative or absolute)");
        opts.addOption("l","lines", true, "Lines to check (comma separated and ranges allowed)");
        opts.addOption("j","jars", true, "Extra jar paths (comma separated or repeatable)");
        opts.addOption("V","include-found", false, "Include all found methods (verbose)");
        opts.addOption("q","quiet", false, "Suppress progress logs");
        opts.addOption("h","help", false, "Print help");

        CommandLineParser parser = new DefaultParser();
        try {
            CommandLine cmd = parser.parse(opts, args);
            if (cmd.hasOption("h")) { printHelp(opts); return; }
            if (!cmd.hasOption("p") || !cmd.hasOption("f") || !cmd.hasOption("l")) {
                System.err.println("Missing required args: -p -f -l");
                printHelp(opts);
                return;
            }

            String projectRoot = cmd.getOptionValue("p");
            String targetFile = cmd.getOptionValue("f");
            String[] lineArgs = cmd.getOptionValues("l");
            String[] extraJars = cmd.getOptionValues("j");
            boolean includeFound = cmd.hasOption("V");
            boolean quiet = cmd.hasOption("q");

            MCParser app = new MCParser();
            app.includeFound = includeFound; 
            app.quiet = quiet;
            app.run(projectRoot, targetFile, lineArgs, extraJars);
        } catch (ParseProblemException pe) {
            System.err.println("Failed to parse CLI args: " + pe.getMessage());
            printHelp(opts);
        }
    }

    private boolean isExternal(String className) {
        if (className == null) return true;
        for (String prefix : EXTERNAL_PREFIXES) if (className.startsWith(prefix)) return true;
        return false;
    }
    
    private void run(String projectRootStr, String targetFileStr, String[] lineArgs, String[] extraJars) throws Exception {
        Path projectRoot = Paths.get(projectRootStr).toAbsolutePath().normalize();
        if (!Files.isDirectory(projectRoot)) { err("Project root not found: " + projectRoot); return; }

        Path targetFile = resolveTargetFile(projectRoot, targetFileStr);
        if (targetFile == null || !Files.exists(targetFile)) { err("Target file not found: " + targetFileStr); return; }

        changedRanges = parseLineArgs(lineArgs);

        CombinedTypeSolver typeSolver = buildTypeSolver(projectRoot, extraJars);
        JavaSymbolSolver symbolSolver = new JavaSymbolSolver(typeSolver);
        ParserConfiguration cfg = new ParserConfiguration().setSymbolResolver(symbolSolver);
        cfg.setLexicalPreservationEnabled(true);
        cfg.setStoreTokens(true);

        parseAllJavaFilesParallel(projectRoot, cfg);

        int threads = Math.max(1, Runtime.getRuntime().availableProcessors());
        buildAllMethodsMapWithResolution(projectRoot, threads);
        buildCallGraphWithResolution(threads);

        Map<String, Set<String>> classToMethods = new HashMap<>();
        for (MethodInfo mi : allMethods.values()) classToMethods.computeIfAbsent(mi.className, k -> new LinkedHashSet<>()).add(mi.simpleName);

        Set<MethodInfo> affectedMethods = allMethods.values().stream()
                .filter(mi -> projectRoot.resolve(mi.sourceFile).normalize().equals(targetFile))
                .filter(mi -> rangesOverlap(mi.startLine, mi.endLine, changedRanges))
                .collect(Collectors.toCollection(LinkedHashSet::new));

        Set<String> affectedMethodKeys = affectedMethods.stream()
                .map(mi -> mi.key).collect(Collectors.toCollection(LinkedHashSet::new));

        Set<String> affectedClasses = new LinkedHashSet<>();
        Set<String> tests = new LinkedHashSet<>();
        Set<String> callerClasses = new LinkedHashSet<>();
        Set<String> calleeClasses = new LinkedHashSet<>();

        Set<String> callerMethodKeys = new LinkedHashSet<>();
        Set<String> calleeMethodKeys = new LinkedHashSet<>();

        for (MethodInfo method : affectedMethods) {
            if(isExternal(method.className)) continue;
            
            if (method.isTest) {
                tests.add(method.className);
            } else {
                affectedClasses.add(method.className);
            }

            Set<String> callersOfThis = reverseCallGraph.getOrDefault(method.key, Collections.emptySet());
            callerMethodKeys.addAll(callersOfThis);

            Set<String> calleesOfThis = callGraph.getOrDefault(method.key, Collections.emptySet());
            calleeMethodKeys.addAll(calleesOfThis);
        }

        for (String mk : callerMethodKeys) {
            String cls = classFromMethodKey(mk);
            if (isExternal(cls)) continue;

            if (Boolean.TRUE.equals(testClassCache.get(cls))) {
                boolean targetFileIsTest = affectedMethods.stream().anyMatch(mi -> mi.isTest);                
                if (!targetFileIsTest) tests.add(cls);
                
            } else {
                callerClasses.add(cls);
            }
        }

        for (String mk : calleeMethodKeys) {
            String cls = classFromMethodKey(mk);
            if (isExternal(cls)) continue;

            if (Boolean.TRUE.equals(testClassCache.get(cls))) {
                boolean targetFileIsTest = affectedMethods.stream().anyMatch(mi -> mi.isTest);                
                if (!targetFileIsTest) tests.add(cls);
                
            } else {
                calleeClasses.add(cls);
            }
        }

        Set<String> productionClasses = new HashSet<>();
        productionClasses.addAll(affectedClasses);
        productionClasses.addAll(callerClasses);
        productionClasses.addAll(calleeClasses);
        
        Set<String> allKnownClasses = classToMethods.keySet(); 

        for (String prodClass : productionClasses) {
            Set<String> namesToTest = new HashSet<>();

            String simpleName = prodClass.substring(prodClass.lastIndexOf('.') + 1);
            namesToTest.add(simpleName);

            String[] parts = prodClass.split("\\.");
            if (parts.length > 1) {
                String potentialParent = parts[parts.length - 2];
                if (!potentialParent.isEmpty() && Character.isUpperCase(potentialParent.charAt(0))) {
                    namesToTest.add(potentialParent);
                }
            }

            for (String nameToMatch : namesToTest) {
                for (String candidate : allKnownClasses) {
                    // Só nos interessa se for classe de teste
                    if (!Boolean.TRUE.equals(testClassCache.get(candidate))) continue;

                    String candidateSimple = candidate.substring(candidate.lastIndexOf('.') + 1);
                    if (candidateSimple.equals(nameToMatch + "Test") || 
                        candidateSimple.equals(nameToMatch + "Tests") ||
                        candidateSimple.equals("Test" + nameToMatch) ||
                        candidateSimple.equals(nameToMatch + "IT")) {
                        tests.add(candidate);
                    }
                }
            }
        }

        List<ClassReport> affectedReports = buildReportsForClasses(affectedClasses, affectedMethodKeys, classToMethods);
        List<ClassReport> callerReports = buildReportsForClasses(callerClasses, callerMethodKeys, classToMethods);
        List<ClassReport> calleeReports = buildReportsForClasses(calleeClasses, calleeMethodKeys, classToMethods);

        Map<String,Object> out = new LinkedHashMap<>();
        if (includeFound) out.put("allMethods", allMethods.keySet());
        out.put("affectedClass", affectedReports);
        out.put("callerClass", callerReports);
        out.put("calleeClass", calleeReports);
        out.put("tests", tests);

        System.out.println(GSON.toJson(out));
    }

    private List<ClassReport> buildReportsForClasses(Set<String> classes, Set<String> methodsToMutateGlobal, Map<String, Set<String>> classToMethods) {
        List<ClassReport> reports = new ArrayList<>();
        for (String cls : classes) {
            Set<String> methodsToMutate = methodsToMutateGlobal.stream()
                    .filter(m -> cls.equals(classFromMethodKey(m)))
                    .map(this::simpleMethodNameFromKey)
                    .collect(Collectors.toCollection(LinkedHashSet::new));

            Set<String> allInClass = classToMethods.getOrDefault(cls, Collections.emptySet());
            Set<String> methodsToExclude = new LinkedHashSet<>(allInClass);
            methodsToExclude.removeAll(methodsToMutate);
            reports.add(new ClassReport(cls, methodsToMutate, methodsToExclude));
        }
        return reports;
    }

    private void parseAllJavaFilesParallel(Path projectRoot, ParserConfiguration cfg) throws IOException {
        List<Path> files = Files.walk(projectRoot).filter(p -> p.toString().endsWith(".java")).collect(Collectors.toList());
        err("Will parse " + files.size() + " files (parallel with symbol solver)");
        ExecutorService exec = Executors.newVirtualThreadPerTaskExecutor();
        try {
            List<CompletableFuture<Void>> futures = files.stream().map(path -> CompletableFuture.runAsync(() -> {
                try {
                    JavaParser parser = new JavaParser(cfg);
                    ParseResult<CompilationUnit> pr = parser.parse(path);
                    pr.getResult().ifPresent(cu -> {
                        try { cu.setStorage(path); } catch (Throwable ignored) {}
                        compilationUnits.put(path.toAbsolutePath().normalize(), cu);
                    });
                } catch (Throwable t) { err("Parse failed for " + path + ": " + t.getMessage()); }
            }, exec)).collect(Collectors.toList());
            CompletableFuture.allOf(futures.toArray(new CompletableFuture[0])).join();
        } 
        finally { 
            exec.shutdown(); 
            try { 
                exec.awaitTermination(1, TimeUnit.MINUTES); 
            } catch (InterruptedException ignored) {} 
        }
        err("Parsed compilation units: " + compilationUnits.size());
    }

    private CombinedTypeSolver buildTypeSolver(Path projectRoot, String[] extraJars) {
        CombinedTypeSolver typeSolver = new CombinedTypeSolver();
        typeSolver.add(new ReflectionTypeSolver());
        try { 
            typeSolver.add(new JavaParserTypeSolver(projectRoot.toFile())); 
            err("Added JavaParserTypeSolver for project root: " + projectRoot); 
        } catch (Throwable ignored) {}
        try { 
            Files.walk(projectRoot)
                .filter(p -> p.getFileName()!=null && p.getFileName().toString().equals("java"))
                .forEach(javaDir -> { 
                    try { 
                        typeSolver.add(new JavaParserTypeSolver(javaDir.toFile())); 
                        err("Added JavaParserTypeSolver for source root: " + javaDir); 
                    } catch (Throwable t) {} 
                }
            ); 
        } catch (IOException ignored) {}

        List<Path> jarDirs = Arrays.asList(projectRoot.resolve("lib"), projectRoot.resolve("libs"));
        for (Path d : jarDirs)
            if (Files.isDirectory(d)) 
                try (Stream<Path> s = Files.list(d)) { 
                    s.filter(p -> p.toString().endsWith(".jar")).forEach(j -> addJarToSolver(j, typeSolver)); 
                } catch (IOException ignored) {}   

        if (extraJars != null) 
            for (String je : extraJars) 
                if (je != null) 
                    for (String part : je.split(",")) { 
                        part = part.trim(); 
                        if (part.isEmpty()) continue; 
                        Path jpath = Paths.get(part); 
                        if (!jpath.isAbsolute()) jpath = projectRoot.resolve(jpath).normalize(); 
                        if (Files.exists(jpath) && jpath.toString().endsWith(".jar")) addJarToSolver(jpath, typeSolver); 
                    }

        Path pom = projectRoot.resolve("pom.xml");
        if (Files.exists(pom)) 
            try { 
                javax.xml.parsers.DocumentBuilderFactory dbf = javax.xml.parsers.DocumentBuilderFactory.newInstance(); 
                javax.xml.parsers.DocumentBuilder db = dbf.newDocumentBuilder(); 
                org.w3c.dom.Document doc = db.parse(pom.toFile()); 
                org.w3c.dom.NodeList deps = doc.getElementsByTagName("dependency"); 
                Path m2 = Paths.get(System.getProperty("user.home"), ".m2", "repository"); 
                for (int i=0;i<deps.getLength();i++) { 
                    org.w3c.dom.Node n = deps.item(i); 
                    if (n.getNodeType()!=org.w3c.dom.Node.ELEMENT_NODE) continue; 
                    org.w3c.dom.Element e = (org.w3c.dom.Element) n; 
                    String gid = getChildText(e, "groupId"); 
                    String aid = getChildText(e, "artifactId"); 
                    String ver = getChildText(e, "version"); 
                    if (gid==null||aid==null||ver==null) continue; 
                    Path jarPath = m2.resolve(gid.replace('.', File.separatorChar)).resolve(aid).resolve(ver).resolve(aid + "-" + ver + ".jar"); 
                    if (Files.exists(jarPath)) addJarToSolver(jarPath, typeSolver); 
                } 
            } catch (Exception ex) { err("Warning: failed to parse pom.xml: " + ex.getMessage()); }

        return typeSolver;
    }

    private static void addJarToSolver(Path jar, CombinedTypeSolver solver) { 
        try { 
            if (Files.exists(jar)) solver.add(new JarTypeSolver(jar.toFile())); 
        } catch (Throwable ignored) {} 
    }

    private void buildAllMethodsMapWithResolution(Path projectRoot, int threads) {
        // First pass: collect methods with simple fallback keys
        for (Map.Entry<Path, CompilationUnit> en : compilationUnits.entrySet()) {
            Path path = en.getKey(); 
            CompilationUnit cu = en.getValue();
            
            cu.findAll(MethodDeclaration.class).forEach(m -> {
                int start = m.getBegin().map(p -> p.line).orElse(-1);
                int end = m.getEnd().map(p -> p.line).orElse(-1);
                
                if (start <= 0 || end <= 0) return;
                
                String className = m.findAncestor(ClassOrInterfaceDeclaration.class)
                        .flatMap(ClassOrInterfaceDeclaration::getFullyQualifiedName)
                        .orElseGet(() -> m.findAncestor(ClassOrInterfaceDeclaration.class)
                                .map(c -> c.getNameAsString()).orElse(null));

                if (className == null || isExternal(className)) return;
                
                String simple = m.getNameAsString();
                int paramCount = m.getParameters().size();
                String fallbackKey = className + "." + simple + "/" + paramCount;
                
                // Compute test flag once per class and cache
                Boolean isTestFlag = testClassCache.computeIfAbsent(className, k -> {
                    ClassOrInterfaceDeclaration cid = m.findAncestor(ClassOrInterfaceDeclaration.class).orElse(null);
                    return computeIsTestClassForDeclaration(cid, cu, path);
                });

                MethodInfo mi = new MethodInfo(fallbackKey, className, simple, isTestFlag, 
                        projectRoot.relativize(path).toString(), start, end);
                
                String declId = nodeId(path, m);
                resolvedMethodCache.putIfAbsent(declId, fallbackKey);
                allMethods.putIfAbsent(fallbackKey, mi);
            });
        }

        err("First pass collected: " + allMethods.size() + " methods");

        // Second pass: resolve methods and update with better keys if possible
        ExecutorService pool = Executors.newFixedThreadPool(threads);
        List<Callable<Void>> tasks = new ArrayList<>();
        
        for (Map.Entry<Path, CompilationUnit> en : compilationUnits.entrySet()) {
            Path path = en.getKey(); 
            CompilationUnit cu = en.getValue();
            
            tasks.add(() -> {
                for (MethodDeclaration m : cu.findAll(MethodDeclaration.class)) {
                    String declId = nodeId(path, m);
                    String fallbackKey = resolvedMethodCache.get(declId);
                    
                    if (fallbackKey == null) continue;
                    
                    try {
                        ResolvedMethodDeclaration rmd = m.resolve();
                        String resolvedSig = safeQualifiedSignature(rmd);
                        String fqClass = miClassName(rmd);
                        
                        // Se conseguiu resolver melhor, atualizar entrada
                        if (!isExternal(fqClass) && !resolvedSig.equals(fallbackKey)) {
                            MethodInfo mi = allMethods.remove(fallbackKey);
                            if (mi != null) {
                                // Preservar line numbers originais (já estão corretos)
                                MethodInfo resolvedMi = new MethodInfo(
                                    resolvedSig, 
                                    fqClass, 
                                    rmd.getName(), 
                                    mi.isTest, 
                                    mi.sourceFile, 
                                    mi.startLine, 
                                    mi.endLine
                                );
                                allMethods.putIfAbsent(resolvedSig, resolvedMi);
                                resolvedMethodCache.put(declId, resolvedSig);
                            }
                        }
                    } catch (Throwable ex) { 
                        // Resolution failed, keep fallback key
                    }
                }
                return null;
            });
        }
        
        try { 
            pool.invokeAll(tasks); 
        } catch (InterruptedException ignored) {} 
        finally { 
            pool.shutdown(); 
            try { 
                pool.awaitTermination(5, TimeUnit.MINUTES); 
            } catch (InterruptedException ignored) {} 
        }

        err("Indexed methods: " + allMethods.size());
    }

    private void buildCallGraphWithResolution(int threads) {
        ExecutorService pool = Executors.newFixedThreadPool(threads);
        List<Callable<Void>> tasks = new ArrayList<>();
        
        for (Map.Entry<Path, CompilationUnit> en : compilationUnits.entrySet()) {
            Path path = en.getKey(); 
            CompilationUnit cu = en.getValue();
            
            tasks.add(() -> {
                for (MethodDeclaration m : cu.findAll(MethodDeclaration.class)) {
                    String callerId = nodeId(path, m);
                    String callerKey = resolvedMethodCache.get(callerId);

                    if (callerKey == null) continue;
                    
                    String callerClass = classFromMethodKey(callerKey);

                    if (isExternal(callerClass)) continue;
                    
                    for (MethodCallExpr call : m.findAll(MethodCallExpr.class)) {
                        String callId = nodeId(path, call);
                        String calleeKey = resolvedCallCache.get(callId);
                        
                        if (calleeKey == null) {
                            try {
                                ResolvedMethodDeclaration rcallee = call.resolve();
                                calleeKey = safeQualifiedSignature(rcallee);
                                
                                // Validar que o método existe em allMethods
                                if (!allMethods.containsKey(calleeKey)) {
                                    // Tentar criar fallback key
                                    String fqClass = miClassName(rcallee);
                                    if (isExternal(fqClass)) {
                                        continue;
                                    }
                                    continue;
                                }
                            } catch (Throwable ex) {
                                // resolution failed, skip this call
                                continue;
                            }
                            resolvedCallCache.put(callId, calleeKey);
                        }
                        
                        if (calleeKey == null) continue;
                        
                        String calleeClass = classFromMethodKey(calleeKey);
                        
                        if (isExternal(calleeClass)) continue;
                        
                        if (!allMethods.containsKey(callerKey) || !allMethods.containsKey(calleeKey)) {
                            continue;
                        }
                        
                        callGraph.computeIfAbsent(callerKey, 
                            k -> Collections.newSetFromMap(new ConcurrentHashMap<>()))
                            .add(calleeKey);
                        
                        reverseCallGraph.computeIfAbsent(calleeKey, 
                            k -> Collections.newSetFromMap(new ConcurrentHashMap<>()))
                            .add(callerKey);
                    }
                }
                return null;
            });
        }
        
        try { 
            pool.invokeAll(tasks); 
        } catch (InterruptedException ignored) {} 
        finally { 
            pool.shutdown(); 
            try { 
                pool.awaitTermination(5, TimeUnit.MINUTES); 
            } catch (InterruptedException ignored) {} 
        }
        
        err("Built call graph. callers: " + callGraph.size() + ", reverse: " + reverseCallGraph.size());
    }

    private static String nodeId(Path path, com.github.javaparser.ast.Node n) {
        Optional<com.github.javaparser.Range> r = n.getRange();
        if (r.isPresent()) {
            com.github.javaparser.Range R = r.get();
            return path.toAbsolutePath().toString() + "@" +
                R.begin.line + ":" + R.begin.column + "-" + R.end.line + ":" + R.end.column;
        } else {
            String snippet = n.toString().length() > 80 ? n.toString().substring(0,80) : n.toString();
            return path.toAbsolutePath().toString() + "@?@" + System.identityHashCode(n) + ":" + snippet.hashCode();
        }
    }

    private static String safeQualifiedSignature(ResolvedMethodDeclaration rmd) {
        try { 
            return rmd.getQualifiedSignature();
        } catch (Throwable t) {
            StringBuilder sb = new StringBuilder(); sb.append(rmd.getQualifiedName()).append("("); 
            List<String> params = new ArrayList<>(); 
            for (int i=0;i<rmd.getNumberOfParams();i++) { 
                try { 
                    params.add(rmd.getParam(i).getType().describe()); 
                } catch (Exception e) { params.add("?"); } 
            } 
            sb.append(String.join(",", params)).append(")"); 
            return sb.toString(); 
        }
    }

    private static String miClassName(ResolvedMethodDeclaration rmd) {
        try { 
            return rmd.getPackageName() + "." + rmd.getClassName(); 
        } catch (Throwable t) { 
            return rmd.getQualifiedName(); 
        } 
    }

    private static Map<Integer,Integer> parseLineArgs(String[] args) { 
        Map<Integer,Integer> ranges = new TreeMap<>(); 
        if (args==null) return ranges; 
        for (String a: args) { 
            if (a==null) continue; 
            for (String token : a.split(",")) { 
                token = token.trim(); 
                if (token.isEmpty()) continue; 
                if (token.contains("-")) { 
                    String[] p = token.split("-"); 
                    try { 
                        int s=Integer.parseInt(p[0].trim()), e=Integer.parseInt(p[1].trim()); 
                        if (s>e){ 
                            int tmp=s; 
                            s=e; 
                            e=tmp; 
                        } 
                        ranges.put(s,e); 
                    } catch (NumberFormatException nfe) { errStatic("Ignored invalid range: "+token); } 
                } else { 
                    try { 
                        int n=Integer.parseInt(token); 
                        ranges.put(n,n); 
                    } catch (NumberFormatException nfe) { errStatic("Ignored invalid line: "+token); } 
                } 
            } 
        } 
        return ranges; 
    }

    private static boolean rangesOverlap(int start, int end, Map<Integer,Integer> changedRanges) { 
        if (changedRanges==null||changedRanges.isEmpty()) return false; 
        
        for (Map.Entry<Integer,Integer> e: changedRanges.entrySet()) { 
            int s=e.getKey(), en=e.getValue(); 

            if (s <= end && en >= start) return true;
        } 
        
        return false;
    }

    private static String getChildText(org.w3c.dom.Element e, String name) { 
        org.w3c.dom.NodeList nl = e.getElementsByTagName(name); 
        if (nl.getLength()==0) return null; 
        org.w3c.dom.Node n = nl.item(0); 
        if (n==null) return null; 
        return n.getTextContent().trim(); 
    }

    private static Path resolveTargetFile(Path projectRoot, String targetFileStr) { 
        Path t = Paths.get(targetFileStr); 
        try { 
            if (t.isAbsolute() && Files.exists(t)) return t.toAbsolutePath().normalize(); 
            Path cwd = Paths.get(".").toAbsolutePath().normalize(); 
            Path cand = cwd.resolve(t).normalize(); 
            if (Files.exists(cand)) return cand; 
            cand = projectRoot.resolve(t).normalize(); 
            if (Files.exists(cand)) return cand; 
            Path projectFileName = projectRoot.getFileName(); 
            if (projectFileName!=null) { 
                String prefix = projectFileName.toString() + File.separator; 
                if (targetFileStr.startsWith(prefix)) { 
                    String stripped = targetFileStr.substring(prefix.length()); 
                    cand = projectRoot.resolve(stripped).normalize(); 
                    if (Files.exists(cand)) return cand; 
                } 
            } 
        } catch (Exception e) {} return null; 
    }

    private static void printHelp(Options opts) { 
        HelpFormatter hf = new HelpFormatter(); 
        hf.printHelp("java MCParser -p <project> -f <file> -l <lines>", opts); 
    }

    private void err(String msg) { if (!quiet) System.err.println(msg); }
    private static void errStatic(String msg) { System.err.println(msg); }

    private static class MethodInfo { 
        final String key; 
        final String className; 
        final String simpleName; 
        final boolean isTest; 
        final String sourceFile; 
        final int startLine; 
        final int endLine; 
        MethodInfo(String key, String className, String simpleName, boolean isTest, String sourceFile, int startLine, int endLine) { 
            this.key=key; 
            this.className=className; 
            this.simpleName=simpleName; 
            this.isTest = isTest; 
            this.sourceFile=sourceFile; 
            this.startLine=startLine; 
            this.endLine=endLine; 
        } 
    }

    private static class ClassReport {
        String className; 
        Set<String> methodsToMutate; 
        Set<String> methodsToExclude; 
        ClassReport(String className, Set<String> methodsToMutate, Set<String> methodsToExclude) { 
            this.className=className; 
            this.methodsToMutate=methodsToMutate; 
            this.methodsToExclude=methodsToExclude;  
        }
    }

    private String classFromMethodKey(String methodKey) { 
        if (methodKey == null) return "<unknown>"; 
        int paren = methodKey.indexOf('('); String before = paren > 0 ? methodKey.substring(0, paren) : methodKey; 
        int lastDotBeforeMethod = before.lastIndexOf('.'); 
        if (lastDotBeforeMethod <= 0) return before; 
        return before.substring(0, lastDotBeforeMethod); 
    }

    private String simpleMethodNameFromKey(String methodKey) { 
        if (methodKey == null) return ""; 
        int paren = methodKey.indexOf('('); 
        String before = paren > 0 ? methodKey.substring(0, paren) : methodKey; 
        int dot = before.lastIndexOf('.'); 
        String name = dot >= 0 ? before.substring(dot + 1) : before; 
        return name; 
    }

    private boolean computeIsTestClassForDeclaration(ClassOrInterfaceDeclaration cls, CompilationUnit cu, Path path) {
        int score = 0;
        String p = path.toString().replace("\\", "/").toLowerCase();
        if (p.contains("/src/test/") || p.contains("/test/")) score += 3;

        // imports
        for (ImportDeclaration imp : cu.getImports()) {
            String im = imp.getNameAsString();
            for (String prefix : TEST_ANNOT_PKG_PREFIX) { if (im.startsWith(prefix)) { score += 2; break; } }
        }

        // class name heuristics
        String simpleName = cls == null ? "" : cls.getNameAsString().toLowerCase();
        if (simpleName.endsWith("test") || simpleName.endsWith("tests") || simpleName.endsWith("it") || simpleName.endsWith("spec")) score += 2;
        if (simpleName.contains("test")) score += 1;

        // annotations on class or methods
        if (cls != null) { if (hasTestAnnotationOnClassOrMethods(cls)) score += 4; }

        return score >= 3;
    }

    private boolean hasTestAnnotationOnClassOrMethods(ClassOrInterfaceDeclaration cls) {
        for (AnnotationExpr ann : cls.getAnnotations()) if (isTestAnnotation(ann)) return true;
        for (MethodDeclaration m : cls.getMethods()) for (AnnotationExpr ann : m.getAnnotations()) if (isTestAnnotation(ann)) return true;
        return false;
    }

    private boolean isTestAnnotation(AnnotationExpr ann) {
        String name = ann.getName().getIdentifier();
        if (TEST_ANNOT_SIMPLE.contains(name)) return true;
        try { 
            ResolvedAnnotationDeclaration rad = ann.resolve(); 
            String q = rad.getQualifiedName(); 
            for (String prefix : TEST_ANNOT_PKG_PREFIX) 
                if (q.startsWith(prefix)) return true; 
                if (q.toLowerCase().contains(".test")) return true; 
        } catch (Throwable e) { /* ignore resolution failure */ }
        return false;
    }
}
