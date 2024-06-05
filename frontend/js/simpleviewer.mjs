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

async function loadPDFtoViewer(url)
{
    const loadingTask = pdfjsLib.getDocument({
        'url': url,
        'cMapUrl': CMAP_URL,
        'cMapPacked': CMAP_PACKED,
        'enableXfa': false,
    });
    const pdfDocument = await loadingTask.promise;
    pdfViewer.setDocument(pdfDocument);
    pdfLinkService.setDocument(pdfDocument, null);
}

globalThis.pdfViewer = pdfViewer;
globalThis.loadPDFtoViewer = loadPDFtoViewer;
