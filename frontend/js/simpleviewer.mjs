pdfjsLib.GlobalWorkerOptions.workerSrc = "js/pdf.worker.min.mjs";

// Some PDFs need external cmaps.
const CMAP_URL = "cmaps/";
const CMAP_PACKED = true;

const container = document.getElementById("pdf-viewer");
const eventBus = new pdfjsViewer.EventBus();

// enable hyperlinks within PDF files.
const pdfLinkService = new pdfjsViewer.PDFLinkService({
    'eventBus': eventBus,
    'externalLinkEnabled': true,
    'externalLinkRel': 'noopener noreferrer nofollow',
    'externalLinkTarget': 2 // Blank
});

// enable find controller.
const pdfFindController = new pdfjsViewer.PDFFindController({
    'eventBus': eventBus,
    'linkService': pdfLinkService,
});

const pdfViewer = new pdfjsViewer.PDFViewer({
    'container': container,
    'enableScripting': false,
    'enableWebGL': true,
    'eventBus': eventBus,
    'linkService': pdfLinkService,
    'renderInteractiveForms': false,
    'findController': pdfFindController,
    'textLayerMode': 2
});
pdfLinkService.setViewer(pdfViewer);

eventBus.on("pagesinit", function () {
    pdfViewer.currentScaleValue = "page-width";
});

function _placeMarker(pageView, params) {
    //pageView.outputScale['sx'], pageView.viewport.scale
    params['offsetX'] /= pageView.viewport.rawDims['pageWidth'];
    params['offsetY'] /= pageView.viewport.rawDims['pageHeight'];
    if(!pageView.annotationEditor) {
        let editorLayer = pageView.annotationEditorLayer.annotationEditorLayer;
        editorLayer.pasteEditor(pdfjsLib.AnnotationEditorType.FREETEXT, {});
        let editor = editorLayer.createAndAddNewEditor(params, false);
        editor.contentDiv.innerHTML = "<div style=\"font-size:50px; transform: translate(-100%, -50%);\">&#11162;</div>";
        pageView.annotationEditor = editor;
        editorLayer.pasteEditor(0, {});
    }
    pageView.annotationEditor.x = params['offsetX'];
    pageView.annotationEditor.y = params['offsetY'];
    pageView.annotationEditor.fixAndSetPosition();
}

eventBus.on("annotationeditorlayerrendered", function (event) {
    let pageView = event.source;
    let page_num = event.pageNumber-1;
    let pdfDocument = pageView.renderingQueue.pdfViewer.pdfDocument;
    if(pdfDocument.pendingMarkers[page_num]) {
        _placeMarker(pageView, pdfDocument.pendingMarkers[page_num]);
        delete pdfDocument.pendingMarkers[page_num];
    }
});

async function loadPDFtoViewer(url)
{
    const loadingTask = pdfjsLib.getDocument({
        'url': url,
        'cMapUrl': CMAP_URL,
        'cMapPacked': CMAP_PACKED,
        'enableXfa': false,
    });
    const pdfDocument = await loadingTask.promise;
    pdfDocument.pendingMarkers = {};
    pdfViewer.setDocument(pdfDocument);
    pdfLinkService.setDocument(pdfDocument, null);
}

function placeMarker(page_num, params)
{
    let pageView = pdfViewer._pages[page_num];
    if(pageView && pageView.annotationEditorLayer && pageView.annotationEditorLayer.annotationEditorLayer)
        _placeMarker(pageView, params);
    else
       pdfViewer.pdfDocument.pendingMarkers[page_num] = params;
}

function scrollToPage(page_num) {
    globalThis.pdfViewer.currentPageNumber = page_num+1;
}

globalThis.pdfViewer       = pdfViewer;
globalThis.loadPDFtoViewer = loadPDFtoViewer;
globalThis.scrollToPage    = scrollToPage;
globalThis.placeMarker     = placeMarker;
